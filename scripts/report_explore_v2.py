"""Describe one bounded V2 cycle, including negative/unmatched results."""
import argparse
import collections
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.s1-deps'))
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import explore_v2 as engine
import s2_preregister as registry
from factor_study_data import digest


def table(f):
    def fmt(x):
        if isinstance(x,(float,np.floating)):return '' if not np.isfinite(x) else f'{x:.3f}'
        return str(x)
    return '\n'.join(['|'+'|'.join(f.columns)+'|','|'+'|'.join(['---']*len(f.columns))+'|']+
        ['|'+'|'.join(fmt(x) for x in r)+'|' for r in f.itertuples(index=False,name=None)])


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path);args=p.parse_args()
    run=args.run or ROOT/json.loads((engine.BASE/'latest_run.json').read_text(encoding='utf8'))['path']
    seal=engine.verify();v=json.loads((run/'verification.json').read_text(encoding='utf8'))
    assert v['computation_complete'] and v['candidate_period_evaluations']==48
    summary=pd.read_csv(run/'summary.csv')
    events=pd.read_parquet(run/'events.parquet');controls=pd.read_parquet(run/'controls.parquet')
    assert len(events)==v['raw_event_rows'] and len(controls)==v['control_rows']
    assert not events.duplicated(['fold_id','candidate_id','product','signal_ns']).any()
    assert events.friction_R.le(.25+1e-12).all()
    invalid=0
    for period in engine.PERIODS:
        f=events.loc[events.fold_id==period['fold_id']]
        end=int(pd.Timestamp(period['discovery_end'],tz='Asia/Shanghai').value)
        for h in (6,12,24):
            complete=f.loc[f[f'status_{h}']=='complete']
            invalid+=int((~((complete.entry_ns>=complete.signal_ns)&(complete[f'label_end_ns_{h}']<end))).sum())
    assert invalid==0
    complete=events.loc[events.status_12=='complete'].copy()
    day=complete.groupby(['fold_id','candidate_id','day']).net_R_12.mean().groupby(['fold_id','candidate_id']).mean().rename('raw_day_equal_net_R')
    summary=summary.merge(day,on=['fold_id','candidate_id'],how='left')
    summary.to_csv(run/'evidence.csv',index=False)
    # Validate every persisted match, including the two new matching dimensions.
    cols=['event_id','product','month','minute','side','vol_bin','activity_bin','scale_bin','signal_ns','net_R_12']
    e=events[cols].set_index('event_id').add_prefix('e_');c=controls[cols].set_index('event_id').add_prefix('c_')
    assert e.index.is_unique and c.index.is_unique
    pair_file=pq.ParquetFile(run/'matched_baseline.parquet');bad=0;last=None;carry=set()
    for batch in pair_file.iter_batches(batch_size=65536):
        f=batch.to_pandas().join(e,on='event_id',validate='many_to_one').join(c,on='control_id',validate='many_to_one')
        same=np.ones(len(f),bool)
        for k in ['product','month','minute','side','vol_bin','activity_bin','scale_bin']:same&=f['e_'+k].to_numpy()==f['c_'+k].to_numpy()
        bad+=int((~same|~np.isclose(f.primary_delta_R,f.e_net_R_12-f.c_net_R_12,atol=1e-12)).sum())
        bad+=int(((f.e_product==f.c_product)&(f.e_signal_ns==f.c_signal_ns)).sum())
        bad+=int(f.duplicated(['fold_id','candidate_id','replicate','c_product','c_signal_ns']).sum())
        keys=list(zip(f.fold_id,f.candidate_id,f.replicate));first,final=keys[0],keys[-1]
        if first==last:
            z=f.loc[(f.fold_id==first[0])&(f.candidate_id==first[1])&(f.replicate==first[2])]
            bad+=sum((p,int(t)) in carry for p,t in zip(z.c_product,z.c_signal_ns))
        if final!=last:carry=set()
        z=f.loc[(f.fold_id==final[0])&(f.candidate_id==final[1])&(f.replicate==final[2])]
        carry.update((p,int(t)) for p,t in zip(z.c_product,z.c_signal_ns));last=final
    assert bad==0 and pair_file.metadata.num_rows==v['matched_pair_rows']
    audit=pd.read_csv(run/'data_access.csv')
    assert (audit.end_exclusive<='2025-11-01').all()
    matrix=pd.DataFrame(index=sorted(summary.candidate_id.unique()))
    for period in ('D1','D2'):
        f=summary.loc[summary.fold_id==period].set_index('candidate_id')
        matrix[period+'_n']=f.complete_primary
        matrix[period+'_raw_day_net']=f.raw_day_equal_net_R
        matrix[period+'_matched_net']=f.signal_net_R
        matrix[period+'_match_pct']=f.matched_fraction*100
    matrix=matrix.reset_index(names='candidate_id');matrix.to_csv(run/'candidate_comparison.csv',index=False)
    outcome='本轮没有同一配置同时通过两个区间的完整门槛。' if not v['pass_both_periods'] else '同一配置在两个历史区间通过完整筛选：'+', '.join(v['pass_both_periods'])+'；仍只属受反馈影响的历史线索。'
    diag=summary.groupby('hypothesis_id').agg(candidate_periods=('candidate_id','size'),raw_day_net_positive=('raw_day_equal_net_R',lambda x:int((x>0).sum())),
        matched_net_positive=('signal_net_R',lambda x:int((x>0).sum())),sufficient_matching=('matched_fraction',lambda x:int((x>=.8).sum())))
    diag.to_csv(run/'mechanism_diagnostics.csv')
    first=summary.loc[summary.fold_id=='D1'].set_index('candidate_id')
    second=summary.loc[summary.fold_id=='D2'].set_index('candidate_id')
    contrast=pd.DataFrame(dict(D1_raw_net=first.raw_day_equal_net_R,D2_raw_net=second.raw_day_equal_net_R,
                              D1_matched_fraction=first.matched_fraction,D2_matched_fraction=second.matched_fraction))
    contrast.to_csv(run/'temporal_contrast.csv')
    for family in ('N1','N2','N3'):
        z=summary.loc[summary.hypothesis_id==family].pivot(index='candidate_id',columns='fold_id',values='raw_day_equal_net_R')
        fig,ax=plt.subplots(figsize=(7,4));z.plot.bar(ax=ax,color=['#2b6f91','#db8a3e']);ax.axhline(0,color='black',lw=.8)
        ax.set_ylabel('Net R per date (raw complete events)');ax.set_title(family+' | fixed 12-bar horizon, exchange fees + 1 tick each side')
        fig.tight_layout();fig.savefig(run/(family+'_net.png'),dpi=150);plt.close(fig)
    report=f'''# V2 新机制探索报告

**完成新一轮24个配置×2个历史区间，共48次检验。{outcome}**

## 思路拓展与事前约束

原V1继续保留，不修改候选、结果或失败日志。V2承认已看过V1反馈，独立冻结于{seal['created_utc']}，源数据仍为research_market_history.sqlite。协议和参数见上一层protocol.json、candidate_manifest.json；代码及数据哈希见freeze.json。

三条新机制：

1. **N1 开盘区间接受后的顺势延续**：9:00–9:30形成完整区间，首个突破与开盘方向一致才考虑入场；首次突破不合格也消耗当天机会，不事后等待“更好”的突破。
2. **N2 假突破后的反向回归**：价格刺穿过去12/24根区间后重新收回，反向K线确认，检验被套突破盘的反向压力；同bar双侧刺穿不推测路径。
3. **N3 低趋势环境的均价回归**：ER较低、价格偏离日内成交量加权典型价后，等待反向确认；日内VWAP为5m OHLCV代理，不伪装成tick VWAP。

每机制8组，最多2个连续参数，全部22品种、双向、日盘不隔夜。新增成本门槛在信号当时计算：交易所基础开/平今费加每边1 tick，合计不得超过当前ATR风险尺度的0.25R。N2/N3还预注册了8/12 tick的ATR尺度门槛。这些是新信号定义，未在结果出来后修改。

对照进一步精确匹配成交活跃度（当前量/过去20根均量固定分箱）及ATR/tick尺度；匹配不足不放宽条件。沿用200次匹配、1000次日期分块和24配置Holm修正。主期限固定12根；6/24根仅作描述，没有优化退出方式。

## 时间顺序及验收

D1：2023-11-01至2024-11-01；D2：2024-11-01至2025-11-01（均右开）。两段不重叠，全部24配置在看D1之前即冻结，D2没有新增或重选规则。旧研究和S1市场描述已经暴露，两个区间都不能称完全未见独立验证。2025-11-01以后的新策略结果没有读取。

预注册要求同一候选在两段均满足60事件、30匹配日期、80%最低匹配率、信号净位移为正、相对效应及95%下界为正、Holm<=0.05、三段稳定性条件。48次判断：{json.dumps(v['decisions'],ensure_ascii=False)}。

## 主要诊断

{table(diag.reset_index())}

raw_day_net_positive 表示完整事件按日期等权净位移为正的候选—区间数；matched_net_positive是成功匹配子集口径，两者不能混用。sufficient_matching是达到80%匹配率的次数。正均值只作线索，不代替不确定性与历史稳定性检查。

本轮有两个不同的瓶颈：

- **对照覆盖不足**：48次检验的最低匹配率都未达到80%，故全部记“未证实”，并非48次都已证明机制无效。细分经济尺度和活动度后，同月同品种同一时刻的合格对照有限；没有为取得通过结果而事后放宽分层。
- **原始净位移也未稳定**：D1有 {int(first.raw_day_equal_net_R.gt(0).sum())} 个配置的全部完整事件日期等权净位移>0，D2为 {int(second.raw_day_equal_net_R.gt(0).sum())} 个。这一统计不依赖匹配成败，因此本轮也不能简单归因于“统计门槛太严”。

具体线索：N2-C08在D1约 {first.loc['N2-C08','raw_day_equal_net_R']:+.3f}R，D2约 {second.loc['N2-C08','raw_day_equal_net_R']:+.3f}R；N3-C02从 {first.loc['N3-C02','raw_day_equal_net_R']:+.3f}R变为 {second.loc['N3-C02','raw_day_equal_net_R']:+.3f}R。这里仍是固定12根的原始标签，不是账户收益。假突破反向在早段存在局部线索，但时间变化明显，不能把正段单独拿出来宣称有效。是否来自事前可识别的市场状态，需要后续新版本检验，不能凭年份倒推过滤器。

{table(matrix)}

![N1](N1_net.png)

![N2](N2_net.png)

![N3](N3_net.png)

## 证据边界

本轮原始事件{v['raw_event_rows']:,}条、对照池{v['control_rows']:,}条、匹配对{v['matched_pair_rows']:,}条；候选重叠，不等于独立交易笔数。6项自动回归和C/JD/PB真实前缀的全部24模板检查通过；实际完整标签越界{invalid}条、错误/重复匹配{bad}条。开发期测试夹具错误保留于上一层development_notes.md。

更严格的经济与活动度匹配可能减少样本、导致未证实；不能把无法匹配当作机制已经无效，也不能只汇报正均值。零匹配时匹配指标留空而非填0。完整证据见evidence.csv，所有失败/稀疏配置都保留。

价格路径、撮合可成交性、保证金和组合权益仍未由本轮认证；固定费用和合约规格沿用已披露代理。原始标签没有模拟止盈止损资金曲线，单手600元计划风险只列可行比例，未为凑一手收紧止损。

## 本轮结论

{outcome} 这轮已扩大机制范围，仍按冻结预算停止，未追加隐形参数、挑品种、放宽匹配条件或读取后续新策略收益。值得继续关注的线索需根据两段净位移和匹配质量分别判断；下一版任何规则变化都需重新登记，历史结果保持暴露标记。

复验：`python scripts/explore_v2.py verify`；`python -m unittest discover -s tests -p test_explore_v2.py -v`；报告重建：`python scripts/report_explore_v2.py --run "{run}"`。重新运行研究会创建新目录，不覆盖本轮，也不能当作新增独立证据。
'''
    (run/'exploration_report.md').write_text(report,encoding='utf8')
    registry.write_json(run/'artifact_verification.json',dict(rows_verified=True,invalid_labels=invalid,invalid_pairs=bad,cost_gate_violations=0,
        signal_implementation_frozen=True,after_2025_11_01_strategy_outcomes_read=False,report_source_sha256=digest(Path(__file__))))
    registry.write_json(run/'artifact_hashes.json',{p.name:digest(p) for p in run.iterdir() if p.is_file() and p.name!='artifact_hashes.json'})
    registry.write_json(engine.BASE/'latest_run.json',dict(path=str(run.relative_to(ROOT)),status='report_complete',report=str((run/'exploration_report.md').relative_to(ROOT))))
    print(json.dumps(dict(completed=True,both_period_passes=v['pass_both_periods'],diagnostics=diag.reset_index().to_dict('records')),ensure_ascii=False))


if __name__=='__main__':main()
