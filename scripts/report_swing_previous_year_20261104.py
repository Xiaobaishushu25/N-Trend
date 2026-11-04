"""Summarize the frozen previous-year validation without selecting new rules."""
import bisect
import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import validate_swing_previous_year_20261104 as v
import research_swing_year_20261104 as b
from report_swing_attribution_20261104 import NAMES

OUT=b.ROOT/'outputs/swing-previous-year-20261104'
ORDER=['A_scale_750_m60','A_scale_1250_m80','A_pyramid','B_target2_guard3','A_original_20margin','B_original_20margin']


def read(path):return json.loads(path.read_text(encoding='utf8'))


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    results=read(v.OUT/'results.json');recent=read(v.OUT/'recent-comparable.json')
    frozen=read(v.OUT/'implementation.json')
    for p in [Path(v.g.__file__),Path(v.x.__file__),Path(b.__file__),b.FEES]:assert hashlib.sha256(p.read_bytes()).hexdigest()==frozen[p.name]
    conn=sqlite3.connect(f'file:{b.DB.as_posix()}?mode=ro',uri=True)
    calendar=[r[0] for r in conn.execute("SELECT DISTINCT date(start_ns/1000000000,'unixepoch','+8 hours') FROM bars WHERE period_seconds=3600 AND cast(strftime('%H',start_ns/1000000000,'unixepoch','+8 hours') AS int) BETWEEN 9 AND 14 ORDER BY 1")]
    days=[d for d in calendar if v.START<=d<v.END]
    def assign(ns):
        t=pd.Timestamp(int(ns),tz='UTC').tz_convert('Asia/Shanghai');day=t.strftime('%Y-%m-%d')
        j=bisect.bisect_right(calendar,day) if t.hour>=18 else bisect.bisect_left(calendar,day)
        return calendar[j] if j<len(calendar) else day
    daily=pd.DataFrame(index=pd.Index(days,name='交易日'));products=pd.DataFrame(index=pd.Index(b.SPECS,name='品种'));trades=[];overview=[];costs=[];checks=[];entry_cache={};summary={}
    fig,axes=plt.subplots(2,1,figsize=(11,7),sharex=True,gridspec_kw={'height_ratios':[2,1]})
    chart_names={'A_scale_750_m60':'A balanced','A_scale_1250_m80':'A growth','A_pyramid':'A profitable pyramid','B_target2_guard3':'B cost-aware 2R'}
    for name in ORDER:
        label=v.LABELS[name];row=results[name];base=row['cases']['base'];stress=row['cases']['slip3'];f=base['full'];cur=recent[name]['base']['full']
        eq=pd.read_csv(v.OUT/f'previous-{name}-base-equity.csv',index_col=0).iloc[:,0]
        ts=read(v.OUT/f'previous-{name}-base-fills.json')
        day_series=pd.Series(eq.values,index=[assign(t) for t in eq.index]).groupby(level=0,sort=True).last().reindex(days).ffill().fillna(0.)
        pnl=day_series.diff();pnl.iloc[0]=day_series.iloc[0];daily[label]=pnl
        products[label]=pd.DataFrame(ts).groupby('symbol').net.sum().reindex(b.SPECS,fill_value=0.)
        assert abs(pnl.sum()-sum(t['net'] for t in ts))<1e-6
        assert abs(products[label].sum()-eq.iloc[-1])<1e-6
        assert abs(eq.iloc[-1]-f['net'])<.011
        running_peak=np.maximum.accumulate(np.r_[0.,eq.values])[1:];dd=running_peak-eq.values
        breach=np.flatnonzero(dd>10000)
        first_breach=str(pd.Timestamp(int(eq.index[breach[0]]),tz='UTC').tz_convert('Asia/Shanghai')) if len(breach) else None
        final_status='未通过：验证年亏损'
        if f['max_drawdown']>10000:final_status+='，回撤超20%'
        overview.append([label,cur['net'],f['net'],f['net']/50000,f['max_drawdown'],stress['full']['net'],stress['full']['max_drawdown'],f['trades'],final_status])
        liquidity=[]
        for t in ts:
            trades.append([label,t['symbol'],t['entry_day'],t['exit_day'],t['lots'],t['net'],t['reason'],t['contract']])
            for fill in t['fills']:
                if fill['kind'] not in ['open','add']:continue
                key=(t['contract'],fill['ns'])
                if key not in entry_cache:entry_cache[key]=conn.execute('SELECT open,volume FROM bars WHERE contract=? AND period_seconds=60 AND start_ns=?',key).fetchone()
                quote=entry_cache[key]
                if quote is None or quote[1]<=0:liquidity.append(dict(contract=key[0],ns=key[1],volume=quote[1] if quote else None))
                if quote is not None:assert abs(quote[0]+t['side']*b.SPECS[t['symbol']][1]-fill['price'])<1e-6
        checks.append(dict(name=name,net_reconciled=True,source_hashes_match=True,zero_volume_opening_fills=liquidity,first_drawdown_over10000=first_breach))
        summary[name]=dict(label=label,previous=base,recent=recent[name]['base'],slip3=stress,first_drawdown_over10000=first_breach,status=final_status)
        for scenario,case in row['cases'].items():
            resolution='1分钟' if scenario in ['base','slip3'] else '15分钟辅助'
            costs.append([label,scenario,resolution,case['full']['net'],case['full']['max_drawdown'],case['full']['trades'],case['full']['win_rate']])
        if name in chart_names:
            ix=pd.to_datetime(eq.index.astype('int64'),utc=True).tz_convert('Asia/Shanghai')
            axes[0].plot(ix,50000+eq.values,label=chart_names[name],lw=1.1);axes[1].plot(ix,-dd,label=chart_names[name],lw=1)
    conn.close()
    monthly=daily.groupby(daily.index.str[:7]).sum()
    ptable=pd.DataFrame({'代码':products.index,'品种':[NAMES[s] for s in products.index]})
    for label in daily.columns:ptable[label]=products[label].values
    dtable=daily.copy();dtable.insert(0,'月份',daily.index.str[:7]);dtable=dtable.reset_index()
    data=dict(period=['2024-10-28','2025-10-27'],recent_period=['2025-10-28','2026-10-27'],schemes=list(daily.columns),overview=dict(headers=['方案','近一年净额','前一年净额','前一年收益率','前一年最大回撤','前一年3跳净额','前一年3跳回撤','交易周期数','验证结论'],rows=overview),monthly=dict(headers=['月份']+list(monthly.columns),rows=[[m]+[float(v) for v in row] for m,row in monthly.iterrows()]),products=dict(headers=list(ptable.columns),rows=ptable.values.tolist()),daily=dict(headers=list(dtable.columns),rows=dtable.values.tolist()),trades=dict(headers=['方案','品种','入场交易日','平仓交易日','手数','净盈亏','退出原因','合约'],rows=trades),costs=dict(headers=['方案','情景','执行粒度','净盈亏','最大回撤','交易周期数','胜率'],rows=costs),summary=summary,checks=checks)
    (OUT/'validation.json').write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
    monthly.to_csv(OUT/'previous-monthly.csv',encoding='utf-8-sig');ptable.to_csv(OUT/'previous-product.csv',index=False,encoding='utf-8-sig');dtable.to_csv(OUT/'previous-daily.csv',index=False,encoding='utf-8-sig')
    axes[0].set(title='Frozen-rule backward validation | 2024-10-28 to 2025-10-27 | CNY50,000',ylabel='Minute-close marked equity (CNY)')
    axes[1].set(ylabel='Drawdown from running peak (CNY)');axes[1].axhline(-10000,color='#b32d35',ls='--',label='CNY10,000 drawdown target')
    for ax in axes:ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.autofmt_xdate();fig.tight_layout();fig.savefig(OUT/'previous-year-equity.png',dpi=170);plt.close(fig)
    def md(headers,rows):return '|'+ '|'.join(headers)+'|\n|'+ '|'.join(['---']+['---:']*(len(headers)-1))+'|\n'+'\n'.join('|'+ '|'.join(v if isinstance(v,str) else f'{v:,.2f}' for v in row)+'|' for row in rows)
    mainrows=[[r[0],r[1],r[2],r[3]*100,r[4],r[5],r[6]] for r in overview]
    monrows=[[m]+[float(row[k]) for k in daily.columns[:4]] for m,row in monthly.iterrows()]
    prows=[[NAMES[s]]+[float(products.loc[s,k]) for k in daily.columns[:4]] for s in products.index if any(abs(products.loc[s,k])>1e-8 for k in daily.columns[:4])]
    report='''# 固定六个策略的前一年验证（2026-11-04）

**结论：六个已保存版本在前一年全部亏损；A均衡、A增长的基准回撤均超过20%目标。此前近一年盈利没有在这段较早行情中重复，当前证据不支持继续把它们当作已验证的稳定增长方案。**

## 口径和冻结要求

- 验证交易日2024-10-28至2025-10-27；原研究交易日2025-10-28至2026-10-27。
- 两个年份分别以5万元、空仓开始，不是资金连续滚动两年。保证金统一名义金额20%，基准每边1跳；另按实际每边3跳执行重放，原风险/摩擦资格不变。
- 六个配置直接读取之前保存的protocol，原信号、执行代码及手续费的SHA256与冻结记录一致。没有重选品种、阈值、仓位、止损、目标或退出参数。
- 基准与3跳都用真实合约原始1分钟线完整账户重放；保护位及信号仍只在原定15分钟/交易小时闭合后更新。手续费翻倍、保证金25%为15分钟辅助情景，单独标注。
- 与原研究一样，各合约使用9月1日起的历史预热，主力映射滞后一个已观察交易日；不拼接换月价差计算收益。
- 这是本轮挖掘窗口之外的历史验证，但项目以前可能已经研究过这段数据，不能声称完全未接触的独立样本。

## 年度对照

金额元；收益率和回撤目标均相对初始5万元。回撤为分钟收盘浮动权益口径，不是盘中最坏损失保证。

'''+md(['方案','原研究年净额','前一年净额','前一年收益率%','前一年回撤','前一年3跳净额','前一年3跳回撤'],mainrows)+'''

两个原一手参照也重新按一分钟完整账户计算。最近一年6个账户的15分钟输入回归结果与原保存结果逐一一致，验证了新日期读取器没有改变信号或账户行为。

![前一年账户权益及回撤](previous-year-equity.png)

## 前一年逐月盈亏

月度是交易日末权益变动，含浮盈浮亏；夜盘归后续日盘交易日。首尾10月均不完整，逐月显示四舍五入可能有几分钱尾差。完整六方案在Excel中。

'''+md(['月份']+list(daily.columns[:4]),monrows)+'''

## 前一年逐品种盈亏

只列前四方案至少一个实际有盈亏的品种，完整22品种及两个原版在Excel中。

'''+md(['品种']+list(daily.columns[:4]),prows)+'''

## 判断应如何更新

1. **信号优势尚未跨年成立。** 原A一手也亏2,442.13元，原B一手亏6,710.26元，不能只把问题归因于增长档手数过多。
2. **A增长不能维持此前优先判断。** 前一年净亏11,282.35元，基准最大回撤17,359.10元（占初始本金34.72%），3跳回撤20,045.70元（40.09%），明显超过20%目标。
3. **A均衡也没有通过。** 基准亏7,395.81元、回撤11,665.06元（23.33%）；3跳亏9,811.41元、回撤13,490.66元（26.98%）。
4. A盈利加仓和B成本改良的回撤虽然较小，但验证年同样亏损，没有形成跨年持续正收益证据。
5. 趋势型策略可以有亏损年份，单个亏损年不能证明它永久无效；但这次结果显示明显时段依赖，加大仓位不能替代更可靠的信号优势。两个年份净额相加仍可能为正，也不能抵消增长档在验证年超过回撤目标的问题。

本轮保留所有原冻结版本及失败验证证据，整体下调至研究对照，不接入生产、不根据本验证年改参。若日后调整逻辑，必须另立新版本，并承认本年份已被观察，不能再把它作为新的独立保留集。

## 数据和核验

- 每品种覆盖243个推断交易日。2025-01-27每品种末根聚合线只有14个组成分钟，本次保留实际数据，没有补造缺失分钟；持仓时间按原有观察K线规则推进。
- 铅品种存在1根零成交量15分钟观察；实际开仓/加仓分钟的成交量核查记录于validation.json，不能把零量报价等同于可成交订单。
- 冻结2026-10-22费率快照用于2024—2025历史，未重建券商逐日历史手续费；20%保证金也只是统一假设。
- 原始1分钟组成数与选中聚合线实际组成分钟数完全核对。6个基准及6个3跳账户的开仓、加仓、平仓现金明细与权益终值核对通过；未发现模拟保证金不足。完整亏损路径会继续运行，20%为评价门槛，未事后加入回撤熔断。
- 全部既有执行测试继续通过；规则代码保持冻结哈希。新读取器对最近一年6个控制账户回归一致。数据库只读，原A/B完整归档及之前的报告未修改。

复现：
```powershell
python scripts/validate_swing_previous_year_20261104.py
python scripts/report_swing_previous_year_20261104.py
```

验证protocol、数据覆盖、逐笔成交、权益和成本场景保存在 `reports/swing-previous-year-20261104`；结果表和图保存在本报告同目录。Excel同时列出六个方案的年度、月度、品种、交易日、逐笔及成本情景。
'''
    (OUT/'REPORT.md').write_text(report,encoding='utf8')
    print(json.dumps(dict(zero_volume_openings={r['name']:len(r['zero_volume_opening_fills']) for r in checks},drawdown_breach={r['name']:r['first_drawdown_over10000'] for r in checks}),ensure_ascii=True,indent=2))


if __name__=='__main__':main()
