"""S1 descriptive matrices, standalone heatmaps, actual-data prefix verification."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import s1_market_regimes as s
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pandas.testing import assert_frame_equal
from factor_study_data import Calendar,digest,local

ROOT=s.ROOT


def markdown(frame):
    """Avoid optional tabulate dependency."""
    def fmt(x):
        if isinstance(x,(float,np.floating)):return '' if pd.isna(x) else f'{x:.4f}'
        return str(x)
    rows=['|'+'|'.join(map(str,frame.columns))+'|','|'+'|'.join(['---']*len(frame.columns))+'|']
    rows.extend('|'+'|'.join(fmt(v) for v in row)+'|' for row in frame.itertuples(index=False,name=None))
    return '\n'.join(rows)


def heatmap(table,title,path,percent=False):
    table=table.sort_index().sort_index(axis=1)
    values=table.to_numpy(float)
    fig,ax=plt.subplots(figsize=(15,8))
    im=ax.imshow(np.ma.masked_invalid(values),aspect='auto',cmap='YlGnBu')
    # Edge quarters and the first post-freeze quarter are partially observed.
    labels=[str(c)+('*' if str(c) in ('2023Q4','2026Q4') or ('state share' in title and str(c)=='2024Q3') else '') for c in table.columns]
    ax.set_xticks(range(len(table.columns)),labels,rotation=45,ha='right')
    ax.set_yticks(range(len(table.index)),[str(i) for i in table.index])
    ax.set_title(title,pad=14);fig.colorbar(im,ax=ax,shrink=.8)
    if len(table.columns)<=14:
        midpoint=np.nanmedian(values)
        for i in range(values.shape[0]):
            for j in range(values.shape[1]):
                v=values[i,j]
                if np.isfinite(v):ax.text(j,i,f'{v:.0f}' if percent else f'{v:.2f}',ha='center',va='center',fontsize=7,color='white' if v>midpoint*1.6 else 'black')
    fig.tight_layout();fig.savefig(path,dpi=160);plt.close(fig)


def real_prefix_checks(features,calendar,cfg,thresholds):
    checks=[];base=['contract','start_ns','end_ns','open','high','low','close','volume','constituent_count','complete']
    cols=['er','atr_pct','realized_vol_12','bar_return','gap_pct','volatility_expansion','past20_return','past20_direction',
          'past_day_rv','past20_rv_mean','past_day_rv_percentile20','volume_relative_past20_same_slot',
          'intraday_realized_vol_so_far','market_state']
    cutoff=int(pd.Timestamp('2025-09-03 11:00',tz='Asia/Shanghai').value)
    for product in ('C','JD','PB'):
        f=features.loc[(features['product']==product)&(features.start_ns<cutoff)].sort_values('start_ns').reset_index(drop=True)
        replay=s.bar_features(f[base],cfg['er_bars'],cfg['atr_bars'])
        replay=s.label_calendar(replay,calendar,product+'0')
        replay,_=s.daily_context(replay,calendar,product+'0',cfg['past_days'],cfg['volume_min_history_days'])
        replay=s.classify(replay,thresholds[product])
        assert_frame_equal(f[cols],replay[cols],check_dtype=False,atol=1e-12,rtol=1e-12)
        checks.append(dict(product=product,rows=len(f),cutoff_ns=cutoff,prefix_matches_full=True))
    return checks


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,default=ROOT/'artifacts/s1');args=parser.parse_args()
    out=args.out.resolve();cfg=json.loads((out/'research_config.json').read_text(encoding='utf8'))
    manifest=json.loads((out/'manifest.json').read_text(encoding='utf8'))
    for name,expected in manifest['code_sha256'].items():
        if digest(ROOT/name)!=expected:raise ValueError('feature source changed since execution: '+name)
    for name,expected in manifest['input_sha256'].items():
        if digest(ROOT/name)!=expected:raise ValueError('input changed: '+name)
    features=pd.read_parquet(out/'regime_features.parquet')
    coverage=pd.read_csv(out/'data_coverage.csv');daily=pd.read_csv(out/'daily_market_descriptors.csv')
    thresholds=json.loads((out/'frozen_thresholds.json').read_text(encoding='utf8'))
    protocol=json.loads((ROOT/cfg['session_template']).read_text(encoding='utf8'))
    calendar=Calendar(json.loads((ROOT/cfg['calendar']).read_text(encoding='utf8')),protocol['night_ends'])
    checks=real_prefix_checks(features,calendar,cfg,thresholds)
    unit=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_s1_market_regimes.py','-v'],cwd=ROOT,text=True,capture_output=True)
    (out/'tests.txt').write_text(unit.stdout+unit.stderr,encoding='utf8')
    if unit.returncode:raise ValueError('S1 regressions failed')
    states=features.loc[features.market_state.isin(cfg['states'])]
    state_counts=states.groupby(['product','quarter','market_state']).size().unstack(fill_value=0).reindex(columns=cfg['states'],fill_value=0)
    shares=state_counts.div(state_counts.sum(axis=1),axis=0)*100
    shares.reset_index().to_csv(out/'quarter_state_shares.csv',index=False)
    heatmap(shares['high_volatility'].unstack('quarter'),'High-volatility state share (%) | frozen discovery thresholds',out/'high_volatility_heatmap.png',True)
    quarter=pd.read_csv(out/'quarter_product.csv')
    heatmap(quarter.pivot(index='product',columns='quarter',values='er_median'),'Median ER(12) | continuous-segment completed bars',out/'trend_efficiency_heatmap.png')
    heatmap(quarter.pivot(index='product',columns='quarter',values='atr_pct_median')*10000,'Median ATR(12) / close (basis points)',out/'volatility_heatmap.png')
    # Retrospective daily paths are descriptive labels, never inputs to same-day decisions.
    paths=[]
    valid=features.loc[features.valid_bar&(features.trading_day!='')]
    for (product,day),g in valid.groupby(['product','trading_day'],sort=True):
        value=g.roll_neutral_log_return.to_numpy().cumsum()
        peaks=np.maximum.accumulate(np.r_[0.,value])[1:]
        paths.append(dict(product=product,trading_day=day,observed_path_return_pct=np.expm1(value[-1])*100,
                          observed_path_drawdown_pct=np.max(1-np.exp(value-peaks))*100,
                          session_range_median_pct=g.groupby('history_segment').apply(lambda z:(z.high.max()-z.low.min())/z.open.iloc[0],include_groups=False).median()*100,
                          fee_floor_1tick_each_side_pct=g.cost_floor_with_1tick_each_side_pct.median()*100))
    outcomes=pd.DataFrame(paths).merge(daily[['product','trading_day','day_complete']],on=['product','trading_day'],how='left')
    outcomes.to_csv(out/'daily_market_outcomes.csv',index=False)
    daily['year']=daily.trading_day.str[:4];daily['quarter']=pd.PeriodIndex(daily.trading_day,freq='Q').astype(str)
    daily=daily.merge(outcomes[['product','trading_day','observed_path_return_pct','observed_path_drawdown_pct','session_range_median_pct']],on=['product','trading_day'],how='left')
    for key in ('year','quarter'):
        complete=daily.loc[daily.day_complete]
        matrix=complete.groupby([key,'product']).agg(complete_days=('trading_day','size'),
            daily_rv_median=('realized_vol','median'),observed_day_return_pct_median=('observed_path_return_pct','median'),
            observed_day_path_dd_pct_median=('observed_path_drawdown_pct','median'),session_range_pct_median=('session_range_median_pct','median')).reset_index()
        matrix.to_csv(out/(key+'_daily_matrix.csv'),index=False)
    # Descriptive year-on-year comparison fixed by calendar, never pick best-performing windows.
    comparison=quarter.loc[quarter.quarter.isin(['2025Q3','2026Q3'])].pivot(index='product',columns='quarter',values=['er_median','atr_pct_median'])
    yoy=pd.DataFrame(index=comparison.index)
    yoy['ER_2025Q3']=comparison['er_median']['2025Q3'];yoy['ER_2026Q3']=comparison['er_median']['2026Q3']
    yoy['ER_change']=yoy.ER_2026Q3-yoy.ER_2025Q3
    yoy['ATR_ratio_2026_to_2025']=comparison['atr_pct_median']['2026Q3']/comparison['atr_pct_median']['2025Q3']
    yoy.reset_index().to_csv(out/'year_on_year_descriptors.csv',index=False)
    state_total=states.groupby(['product','market_state']).size().unstack(fill_value=0).reindex(columns=cfg['states'],fill_value=0)
    state_share=state_total.div(state_total.sum(axis=1),axis=0)*100
    state_share.reset_index().to_csv(out/'overall_state_shares.csv',index=False)
    status=features.market_state.value_counts().to_dict()
    warmup=100*status.get('warmup_or_invalid',0)/len(features)
    complete_days=int(daily.day_complete.sum())
    s.save_json(out/'verification.json',dict(s1_passed=True,synthetic_test_methods=9,unit_exit_code=unit.returncode,
        actual_data_prefix_checks=checks,threshold_sources_before_freeze=manifest['temporal_check'],
        duplicate_product_timestamp=int(features.duplicated(['product','start_ns']).sum()),
        classification_counts={k:int(v) for k,v in status.items()},complete_product_days=complete_days,
        s0_full_execution_certification=False,strategies_evaluated=0))
    exposures=pd.DataFrame([dict(stage='S1',product=p,start=cfg['start'],end_exclusive=cfg['end_exclusive'],
        exposure_type='descriptive_market_data_only',strategy_return_access=False,parameter_feedback_rounds=0) for p in coverage['product']])
    exposures.to_csv(out/'data_exposures.csv',index=False)
    definitions='''# S1 feature definitions

`asof_ns=end_ns`: all bar features become available after the 5m bar completes.

|Column|Definition / availability|
|---|---|
|er|abs(Ct-Ct-12) / sum(abs(close.diff()),12); 13 contiguous completed closes, flat path=0|
|atr_pct|mean(TR,12)/current close; first TR of each segment=high-low|
|realized_vol_12|sqrt(sum(log(close/prev_close)^2,12)); contiguous segment only|
|volatility_expansion|current ATR ratio / mean of previous 12 ATR ratios|
|gap_pct|open/previous close-1 inside continuous segment; no roll/overnight jump included|
|roll_neutral_log_return|within segment close-to-close log return; segment first bar log(close/open); invalid=missing|
|past20_return / direction|sum/sign of previous 20 complete trading-day observed returns; overnight and unobserved session gaps excluded|
|past_day_rv_percentile20|previous completed day's RV percentile against 20 earlier complete trading days; current day excluded|
|volume_relative_past20_same_slot|current volume / previous 20 trading days' mean volume at same clock slot; at least 5 observations|
|intraday_realized_vol_so_far|sqrt(cumulative squared roll-neutral observed returns so far this trading day)|
|market_state|after calibration freeze only: high ATR first, then high ER directional, otherwise range|
|fee_floor_roundtrip_cny|fixed dated exchange snapshot open+close_today, same reference price; excludes broker addition|
|bar_range_less_cost_proxy_pct|bar high-low / close minus exchange fee floor and 1 tick each side; amplitude descriptor, not attainable profit|

Daily outcome tables are retrospective labels, never same-day model inputs. Missing calendars yield null daily-context features. All rolling intervals use only past history. No normalizer or classifier is fitted on later data.
'''
    (out/'feature_definitions.md').write_text(definitions,encoding='utf8')
    displayed=coverage[['product','selected_rows','invalid_rows','classified_rows','past20_context_rows']]
    report=f'''# S1 市场状态研究报告

**S1 描述性研究完成，时间因果检查通过。未挖掘策略，未评估交易收益。完整 S0 执行认证仍按原记录保留。**

运行 ID：{manifest['run_id']}。git 基线：`{manifest['git_sha']}`；精确输入、未提交代码和产物哈希见 manifest 与 artifact_hashes。原库只读，唯一数据源为 `{cfg['source']}`。

## 研究口径

接受用户“抓取的是当时主力合约”的来源说明。库内换月附近存在新旧合约重叠，使用已有 mappings 去重筛选；采用 **effective_date 严格早于当前自然日** 的保守可用约定。该约定避免使用未来主力和未来流动性，也可能比供应商实际切换滞后一天；不把静态快照当成已验证历史发布时间。

研究窗口 {cfg['start']} 至 {cfg['end_exclusive']}（右开），实际数据末端为 {coverage.last_end.max()}；不足的尾段未补造。全部 22 品种，未依据表现删选。主库候选合约记录 {int(coverage.raw_rows.sum()):,} 条，主力筛选后 {len(features):,} 条，异常 {int(coverage.invalid_rows.sum()):,} 条保留标记但不参与特征/描述矩阵。

ER/ATR 均固定 12 根 5m。连续段在任何缺口、休市间隔、换月、非法/不完整 bar 处重置，不凭未知日历跨间隙继承价格变化。因此有 **{warmup:.1f}%** 的记录缺少足够连续历史；状态占比只代表可分类时段，夜盘较长的品种更容易满足预热，不能直接解读成全天趋势概率。

## 阈值与可实时性

发现窗口固定为 2023-11-01 至 2024-08-01（前 9 个月）。每品种 ER 的 67% 分位、ATR/close 的 75% 分位只在该窗口计算，2024-08-01 后冻结使用。发现窗口内只标 `calibration_only`，没有用未来得到的阈值给更早时刻生成可交易状态。后续数据不改阈值。

三个状态互斥：① ATR 高于冻结门槛→高波动；②否则 ER 高于冻结门槛→方向性；③否则→区间。状态名称是描述性标签，不能直接等同于某策略适合交易。

过去 20 日波动、方向和同一时刻相对成交量只用已结束交易日。日历覆盖外（2023/2024）保留 bar 级 ER/ATR，交易日及日级上下文留空，不按工作日编造。日内 RV 的当前值只累计至当前 bar。完整交易日使用静态时段模板与覆盖内日历核对；有效完整品种交易日共 {complete_days:,} 个。

**验证：9 项自动回归全部通过；C/JD/PB 三个实际数据前缀截断重算与完整数据同一前缀逐列一致**，涵盖日级历史、同一时刻成交量和状态标签。验证详情见 `verification.json`，特征公式见 `feature_definitions.md`。

## 覆盖与分类结果

{markdown(displayed)}

各状态数量：{json.dumps({k:int(v) for k,v in status.items()},ensure_ascii=False)}。

冻结后各品种状态占比（%）：

{markdown(state_share.reset_index())}

## 近期与更早市场差异

固定比较 2026Q3 与 2025Q3，不根据收益选月份。ATR 比值 >1 表示近期典型波幅更大；ER 变化 >0 表示连续时段价格路径更有方向性。两者均不说明策略能盈利。

几个具体观察：

- EG 的 ATR/价格中位数约为去年同期的 {yoy.loc['EG','ATR_ratio_2026_to_2025']:.2f} 倍，ER 中位数从 {yoy.loc['EG','ER_2025Q3']:.3f} 变为 {yoy.loc['EG','ER_2026Q3']:.3f}。
- PP 的相对波幅约为去年同期的 {yoy.loc['PP','ATR_ratio_2026_to_2025']:.2f} 倍，ER 从 {yoy.loc['PP','ER_2025Q3']:.3f} 变为 {yoy.loc['PP','ER_2026Q3']:.3f}。
- SI 的相对波幅约为去年同期的 {yoy.loc['SI','ATR_ratio_2026_to_2025']:.2f} 倍，ER 从 {yoy.loc['SI','ER_2025Q3']:.3f} 变为 {yoy.loc['SI','ER_2026Q3']:.3f}。

这些差异为后续预注册假设提供描述背景；没有在本阶段用它们挑选品种或调整交易规则。

{markdown(yoy.reset_index())}

按年、季度、品种、状态、时段的完整矩阵见 CSV。`year_daily_matrix.csv` / `quarter_daily_matrix.csv` 仅包含完整且日历覆盖内的交易日；没有把缺失的 2023/2024 日级结果填成 0。

图中星号代表未完整覆盖的季度：2023Q4 从 11 月开始、2026Q4 仅截至 10 月 27 日；状态占比图的 2024Q3 从冻结后的 8 月开始。这些季度不能直接当完整季度进行样本量比较。

![趋势效率](trend_efficiency_heatmap.png)

![波动](volatility_heatmap.png)

![高波动状态占比](high_volatility_heatmap.png)

## 成本及收益/回撤描述的解释

费用采用已有 **2026-10-22 固定交易所基础费率快照**，保留按手/按成交额、开/平今差异，不加期货公司佣金；SR 使用 `published_schedule_before_override`，排除此前人工覆盖费率。该快照多来自交易所标准费率镜像，是研究假设，并非已还原历史账单。

成本只用于“波幅减成本下限”的描述，不生成买卖。换算使用既有规格代理：precision 默认 BU tick=2，既有研究覆盖 JD 乘数=10、SH tick=1。尚未最终核实的规格影响成本列，不影响无量纲 ER/ATR。图表与状态分类不使用成本列。

`daily_market_outcomes.csv` 给出剔除跨段/换月跳变后的已观察价格路径涨跌、路径回撤和典型时段区间，包含完整性标记。它们是市场描述标签，**不是策略收益、账户最大回撤或可实现交易利润**；撮合、保证金和资金约束尚未在此表模拟。

## 本阶段结论与后续

S1 已建立可以按历史时刻重算的基础市场状态表；未发现以全样本分位数、未来合约或未来收盘参与当前分类的情况。数据覆盖、预热缺口和日历缺口均被保留，不以选品种或调阈值消除。

下一步为 S2：预注册机制假设与搜索预算。当前没有据此声称存在有效 Alpha，也没有通过市场状态描述绕过后续 S0 交易执行修复。历史市场状态已被本次查看，记录于 data_exposures.csv；没有读取 S4 策略收益，完整 WFO 反馈次数为 0。

复验命令：

```powershell
python -m pip install --no-deps --target .s1-deps pyarrow=={manifest['runtime']['pyarrow']}
python -m unittest discover -s tests -p test_s1_market_regimes.py -v
python scripts/s1_market_regimes.py
python scripts/report_s1_market_regimes.py
```
'''
    (out/'market_regime_report.md').write_text(report,encoding='utf8')
    manifest['report_code_sha256']={str(p.relative_to(ROOT)):digest(p) for p in [Path(__file__),ROOT/'tests/test_s1_market_regimes.py']}
    manifest['s1_passed']=True;manifest['strategies_evaluated']=0
    s.save_json(out/'manifest.json',manifest)
    s.save_json(out/'artifact_hashes.json',{p.name:digest(p) for p in out.iterdir() if p.is_file() and p.name!='artifact_hashes.json'})
    print(json.dumps(dict(s1_passed=True,rows=len(features),actual_prefix_checks=checks,complete_product_days=complete_days),ensure_ascii=False))


if __name__=='__main__':main()
