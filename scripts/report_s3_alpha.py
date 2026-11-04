"""Report frozen S3 discovery results; never reruns or selects rules."""
import argparse
import collections
import importlib.metadata
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if (ROOT/'.s1-deps').exists():sys.path.insert(0,str(ROOT/'.s1-deps'))
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from factor_study_data import digest
import s2_preregister as prereg


def table(f):
    def fmt(v):
        if isinstance(v,(float,np.floating)):return '' if not np.isfinite(v) else f'{v:.4f}'
        return str(v)
    return '\n'.join(['|'+'|'.join(f.columns)+'|','|'+'|'.join(['---']*len(f.columns))+'|']+
        ['|'+'|'.join(fmt(v) for v in r)+'|' for r in f.itertuples(index=False,name=None)])


def heatmap(summary,path):
    v=summary.pivot(index='candidate_id',columns='fold_id',values='effect').sort_index()
    vals=v.to_numpy(float);limit=max(.01,float(np.nanmax(np.abs(vals)))) if np.isfinite(vals).any() else 1
    fig,ax=plt.subplots(figsize=(10,13));im=ax.imshow(np.ma.masked_invalid(vals),cmap='RdBu_r',vmin=-limit,vmax=limit,aspect='auto')
    ax.set_xticks(range(len(v.columns)),list(v.columns));ax.set_yticks(range(len(v.index)),list(v.index))
    for i,c in enumerate(v.index):
        for j,f in enumerate(v.columns):
            r=summary.loc[(summary.candidate_id==c)&(summary.fold_id==f)].iloc[0]
            mark='*' if r.decision=='alpha_screen_pass_research_only' else ('?' if r.decision=='unconfirmed_insufficient_evidence' else '')
            text=f'{vals[i,j]:+.2f}{mark}' if np.isfinite(vals[i,j]) else 'NA'
            ax.text(j,i,text,ha='center',va='center',fontsize=8,color='white' if abs(vals[i,j])>limit*.6 else 'black')
    ax.set_title('Discovery matched net effect (R)\n* registered screen pass; ? insufficient evidence',pad=14)
    fig.colorbar(im,ax=ax,shrink=.75);fig.tight_layout();fig.savefig(path,dpi=160);plt.close(fig)


def check_pairs_and_secondary(schemas,run,checks):
    metrics=['gross_R','net_R','MFE_R','MAE_R','p1_lower','p1_upper','p2_lower','p2_upper']
    labels=[f'{k}_{h}' for h in (6,12,24) for k in metrics]
    columns=['event_id','signal_ns','product','month','minute','vol_bin','side']+labels
    controls=schemas['baseline_pool'].read(columns=columns).to_pandas().set_index('event_id').add_prefix('c_')
    events=schemas['raw_alpha_events'].read(columns=columns).to_pandas().set_index('event_id').add_prefix('e_')
    assert controls.index.is_unique and events.index.is_unique
    sums=collections.defaultdict(lambda:collections.defaultdict(float));carry=set();last_group=None
    invalid=0
    for batch in schemas['matched_baseline'].iter_batches(batch_size=65536):
        f=batch.to_pandas().join(events,on='event_id',validate='many_to_one').join(controls,on='control_id',validate='many_to_one')
        equal=np.ones(len(f),dtype=bool)
        for k in ('product','month','minute','vol_bin','side'):equal&=f['e_'+k].to_numpy()==f['c_'+k].to_numpy()
        difference=(f.e_net_R_12-f.c_net_R_12).to_numpy()
        invalid+=int((~equal|~np.isclose(difference,f.primary_delta_R.to_numpy(),atol=1e-12)).sum())
        checks['pairs_with_same_event_and_control_time']+=int(((f.e_product==f.c_product)&(f.e_signal_ns==f.c_signal_ns)).sum())
        unique_keys=['fold_id','candidate_id','replicate','c_product','c_signal_ns']
        invalid+=int(f.duplicated(unique_keys).sum())
        groupkeys=list(zip(f.fold_id,f.candidate_id,f.replicate))
        first=groupkeys[0]
        if first==last_group:
            first_rows=f.loc[(f.fold_id==first[0])&(f.candidate_id==first[1])&(f.replicate==first[2])]
            invalid+=sum((p,int(t)) in carry for p,t in zip(first_rows.c_product,first_rows.c_signal_ns))
        final=groupkeys[-1]
        if final!=last_group:carry=set()
        last_rows=f.loc[(f.fold_id==final[0])&(f.candidate_id==final[1])&(f.replicate==final[2])]
        carry.update((p,int(t)) for p,t in zip(last_rows.c_product,last_rows.c_signal_ns));last_group=final
        for h in (6,12,24):
            ok=f[f'e_net_R_{h}'].notna()&f[f'c_net_R_{h}'].notna()
            sub=f.loc[ok,['fold_id','candidate_id']].copy();sub['pairs']=1
            for k in metrics:
                sub['signal_'+k]=f.loc[ok,f'e_{k}_{h}'].to_numpy()
                sub['control_'+k]=f.loc[ok,f'c_{k}_{h}'].to_numpy()
            for (fold,candidate),r in sub.groupby(['fold_id','candidate_id']).sum(numeric_only=True).iterrows():
                target=sums[(fold,candidate,h)]
                for k,v in r.items():target[k]+=float(v)
    result=[]
    for (fold,candidate,h),v in sorted(sums.items()):
        n=v['pairs'];r=dict(fold_id=fold,candidate_id=candidate,horizon_bars=h,matched_pair_records=int(n))
        for k in metrics:
            r['signal_'+k]=v['signal_'+k]/n;r['control_'+k]=v['control_'+k]/n
            r['delta_'+k]=r['signal_'+k]-r['control_'+k]
        result.append(r)
    pd.DataFrame(result).to_csv(run/'secondary_matched_comparison.csv',index=False)
    checks['invalid_or_reused_matched_pairs']=invalid


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run',type=Path);args=parser.parse_args()
    if args.run:run=args.run.resolve()
    else:run=ROOT/json.loads((ROOT/'artifacts/s3/latest_run.json').read_text(encoding='utf8'))['path']
    verify=json.loads((run/'verification.json').read_text(encoding='utf8'))
    if not verify['s3_computation_complete'] or verify['candidate_fold_evaluations']!=144:raise ValueError('S3 incomplete; preserve partial outputs, do not claim completion')
    manifest=json.loads((run/'manifest.json').read_text(encoding='utf8'))
    for name,expected in manifest['source_code_sha256'].items():
        if digest(ROOT/name)!=expected:raise ValueError('frozen implementation changed: '+name)
    rows=json.loads((run/'alpha_summary.json').read_text(encoding='utf8'));summary=pd.read_csv(run/'alpha_summary.csv')
    counts=summary.groupby(['fold_id','hypothesis_id','decision']).size().unstack(fill_value=0).reset_index()
    counts.to_csv(run/'family_fold_decisions.csv',index=False)
    codes={'alpha_screen_pass_research_only':'P','not_passed_registered_screen':'N','unconfirmed_insufficient_evidence':'U'}
    matrix=summary.assign(outcome=summary.decision.map(codes)).pivot(index='candidate_id',columns='fold_id',values='outcome').reset_index()
    matrix.to_csv(run/'candidate_fold_decisions.csv',index=False)
    heatmap(summary,run/'matched_effect_heatmap.png')
    # Product attribution is descriptive, not a new product-selection experiment.
    product=[];blocks=[]
    for r in rows:
        product.extend(dict(fold_id=r['fold_id'],candidate_id=r['candidate_id'],product=k,effect_R=v) for k,v in r['product_effects'].items())
        blocks.extend(dict(fold_id=r['fold_id'],candidate_id=r['candidate_id'],**b) for b in r.get('discovery_blocks',[]))
    pd.DataFrame(product).to_csv(run/'product_effects.csv',index=False)
    pd.DataFrame(blocks).to_csv(run/'discovery_block_effects.csv',index=False)
    access=pd.read_csv(run/'data_access.csv');folds={f['fold_id']:f for f in prereg.folds()}
    for f,g in access.groupby('fold_id'):
        assert set(g.role)=={'Researcher.discovery'}
        assert set(g.start_inclusive)=={folds[f]['discovery_start']}
        assert set(g.end_exclusive)=={folds[f]['discovery_end']}
    # Row-group verification keeps memory bounded for the large matched table.
    schemas={n:pq.ParquetFile(run/(n+'.parquet')) for n in ('raw_alpha_events','baseline_pool','mechanical_breakouts','matched_baseline')}
    row_counts={n:p.metadata.num_rows for n,p in schemas.items()}
    assert row_counts==verify['table_row_counts']
    checks=dict(invalid_complete_labels=0,duplicate_signal_keys=0,pairs_with_same_event_and_control_time=0)
    seen=set();raw_status=collections.Counter();valid_h12=0;raw_event_count=0;calendar_known=0
    check_columns=['fold_id','candidate_id','product','signal_ns','entry_ns','calendar_known','R_price']+[k for h in (6,12,24) for k in (f'status_{h}',f'label_end_ns_{h}',f'net_R_{h}')]
    for batch in schemas['raw_alpha_events'].iter_batches(batch_size=65536,columns=check_columns):
        for r in batch.to_pylist():
            key=(r['fold_id'],r['candidate_id'],r['product'],r['signal_ns'])
            if key in seen:checks['duplicate_signal_keys']+=1
            seen.add(key);raw_status[r['status_12']]+=1;raw_event_count+=1;calendar_known+=r['calendar_known']
            if r['status_12']=='complete':valid_h12+=1
            end=int(pd.Timestamp(folds[r['fold_id']]['discovery_end'],tz='Asia/Shanghai').value)
            for h in (6,12,24):
                if r[f'status_{h}']=='complete':
                    if not (r['signal_ns']<=r['entry_ns']<r[f'label_end_ns_{h}']<end and r['R_price']>0 and np.isfinite(r[f'net_R_{h}'])):checks['invalid_complete_labels']+=1
    check_pairs_and_secondary(schemas,run,checks)
    if any(checks.values()):raise ValueError('S3 artifact integrity checks failed: '+str(checks))
    decisions=collections.Counter(summary.decision);passed=summary.loc[summary.decision=='alpha_screen_pass_research_only']
    sufficiently=summary.loc[summary.decision!='unconfirmed_insufficient_evidence']
    outcome='未发现通过预注册发现期筛选的配置；不进入 S4 策略选择测试。' if passed.empty else f'有 {len(passed)} 个候选×轮次通过发现期筛选，只具备后续研究资格；不能据此认定机制稳定有效或允许实盘。'
    ranges=summary[['fold_id','candidate_id','complete_primary','matched_fraction','matched_days','effect','ci_lower','ci_upper','holm_p','signal_net_R','positive_blocks','decision']]
    ranges.to_csv(run/'primary_evidence.csv',index=False)
    registry=pd.read_csv(run/'experiment_registry.csv')
    assert len(registry)==144 and not registry.duplicated(['fold_id','candidate_id']).any()
    candidates=json.loads((ROOT/'artifacts/s2/candidate_manifest.json').read_text(encoding='utf8'))['candidates']
    registry['candidate_sha256']=registry.candidate_id.map({c['candidate_id']:c['candidate_sha256'] for c in candidates})
    registry['implementation_freeze_sha256']=manifest['implementation_freeze_sha256']
    registry['database_sha256']=manifest['database_sha256'];registry['research_version']='1.0'
    registry.to_csv(run/'experiment_registry_with_versions.csv',index=False)
    counttexts='；'.join(f'{k}={v}' for k,v in decisions.items())
    report=f'''# S3 原始 Alpha 检验报告

**S3 已完成：24 个冻结候选 × 6 个发现窗 = 144 次候选—轮次检验。{outcome}**

运行：`{run.name}`。所有真实结果产生前已冻结信号代码及实现约定，见上一层 implementation_freeze.json；S2 原始文件未修改。唯一数据源为 research_market_history.sqlite，输入哈希 `{manifest['database_sha256']}`。未运行 Selection/Test、未预约完整 WFO，历史暴露状态沿用 S2。

## 本次做了什么

三个机制逐一实现，参数严格来自 S2 的3×8配置。日盘、不隔夜，14:50起不新入场；高周期只取完整时钟桶。午间休市暂停bar计数，异常缺口/换月重置历史。相同品种每个候选24根冷却，未把重叠事件当独立交易。

原始事件 {raw_event_count:,} 条，其中12根标签完整 {valid_h12:,} 条；匹配对照池 {row_counts['baseline_pool']:,} 条；200次匹配共 {row_counts['matched_baseline']:,} 对；H2全部首次突破（包括未确认）{row_counts['mechanical_breakouts']:,} 条。**候选及滚动窗口重叠，这些记录数不等于独立交易数，也不是实际成交。**

12根标签状态：{json.dumps(dict(raw_status),ensure_ascii=False)}。截断、无下一bar、缺口、入场无法确认均保留原因，没有填成零收益。未来标签只用于计分，不影响信号时点或冷却决策。

10项信号/统计合成回归和1项报告匹配校验回归通过。F1 中 C/JD/PB 三个真实数据前缀的全部24模板信号及首次突破重算一致。实际产物再次流式核验：{json.dumps(checks,ensure_ascii=False)}。逐条完整标签（6/12/24根）的入场时间不早于信号收盘、标签严格位于发现期内；匹配两侧分层一致且同次重复无对照重用。开发期测试夹具错误记录保留于上一层 development_failures.md。

## 决策汇总

{counttexts}。

P=通过发现期筛选；U=事件/日期/匹配率不足，未证实；N=达到基础证据规模后未通过冻结统计/成本/稳定性门槛。P不等于可部署。每轮24项主检验使用Holm修正，不把重叠发现窗当六次独立验证。

{table(matrix)}

按机制、轮次汇总见 family_fold_decisions.csv，全部144行指标见 primary_evidence.csv / alpha_summary.csv。没有只留下最好候选或删除失败配置。

## 统计与负对照

主指标固定12根：信号净 forward_R 与匹配基线的差异，每个事件日期等权。匹配精确到品种、月份、5m时刻、方向和事前ATR固定分箱；不匹配就缺失，不看结果后放宽条件。匹配剔除所有24模板的真实信号时刻。200次固定种子重复，匹配率采用其中最低值。

日期分块Bootstrap 1000次，同时按原始日期聚合信号正贡献、对照负贡献，跨品种共同重采样，纳入匹配重复的不确定性。95%区间及居中零分布单侧p值按冻结约定计算，再对每轮24项做Holm修正。没有从6/12/24里挑选最好期限。

基础门槛为60个完整事件、30个匹配日期、80%最低匹配率；主效应及95%下界>0、Holm p<=0.05、3个发现子段至少2段正且各>=10事件、信号本身净位移>0。门槛属于事前筛选约定，不能证明永久有效。

H2首次突破全体单列机械对照，不只保留未来发生回踩的突破；该群体与确认回踩群体构成不同，均值比较只能作诊断，不能当无偏因果估计。分品种和三子段效应另存CSV，只作归因，没有据此新增品种过滤器。

H3假设说明提到活动度匹配，操作性search_budget实际只冻结了品种/月份/时刻/方向/ATR分箱，当前严格执行后者，**没有额外按成交量匹配**。同品种同一时刻仅控制常见的盘中活动节奏，不能完全排除成交量混杂；本次结果不能单独证实H3的“超越成交活跃度”解释。为避免结果反馈后改协议，本次未新增该对照维度。

![发现期匹配净效应](matched_effect_heatmap.png)

## 方向、路径与成本解释

raw_alpha_events.parquet 保存6/12/24根 gross_R、net_R、MFE/MAE、+1R/+2R先于-1R的上下界、同bar歧义和未触达标志。未在期限内触达任一水平记未触达，不从概率分母删除。先观察开盘已穿越水平，再对剩余同bar路径采用先止损；没有假设乐观的tick路径。

R为信号时点ATR12，最早下一bar开盘加1 tick入场；gross_R为该入场参考至终点收盘的方向位移，net_R再计退出不利1 tick和固定交易所开/平今费用。费用来自已登记快照，SR采用发布费率而非旧人工覆盖。JD/SH等规格仍属S1登记代理，没有伪装成已核验历史账单。

经济上超过600元/400元一手计划风险的事件仍保留，经济可行比例单列。这里没有优化止盈止损，没有模拟共享账户资金曲线。即使净位移为正，也仍须实际撮合、保证金和在险约束验收。

风险比例的分母是可以计算入场参考价和结构止损风险的事件；标签截断事件也保留。匹配对照采用通用5m ATR已预热且能形成12根标签的决策时刻，不额外要求满足待检机制的特定形态或高周期预热条件；它不是只从同机制失败信号里挑选的对照。

成本门槛中的signal_net_R是进入主匹配统计的信号日期等权净位移；全体完整事件的普通均值另在net_R_h12。secondary_matched_comparison.csv给出三种期限的匹配对逐条均值、MFE/MAE和触达概率对比，仅作次要描述，其加权方式不同于日期等权主检验，不据此重新挑选期限或候选。

## 数据和证据范围

当前bar日历覆盖内比例为 {100*calendar_known/max(1,raw_event_count):.1f}%。2023/2024信号可按已观察日盘模板产生，但历史休市日历和盘口无法在此核验，带有calendar_known/execution_verified标志。所有OHLC入场均为执行代理，execution_verified=false。未知日历间隙未补零，涨跌停排队和真实成交不能由本报告证明。

数据读取审计 data_access.csv 只含逐轮 discovery 起止。后轮发现窗合法包含此前已经发生的市场价格；这不等于读取该轮未来Selection/Test策略收益。S1已观察全区间市场描述、旧反馈史不完整，故任何历史结果都不称完全未见最终验证。

## 交付与复验

- raw_alpha_events.parquet：所有真实信号及逐期限标签；包含失败/截断事件。
- baseline_pool.parquet：对照编号和完整标签；matched_baseline.parquet：200次匹配关系、双侧日期及主差异，按event_id/control_id连接前两表。
- mechanical_breakouts.parquet：H2首次突破全体；bootstrap_effects.csv：日期重采样结果。
- experiment_registry.csv / evaluation_exposures.csv：本次144次发现期反馈，S2冻结初始登记不覆盖；with_versions版本补齐候选/实现/数据哈希，不覆盖原日志。
- verification.json / manifest.json / artifact_hashes.json：时序验证、源版本及产物校验。

```powershell
python scripts/run_s3_alpha.py verify
python -m unittest discover -s tests -p test_s3_alpha.py -v
# 正式计算已经完成；重跑会创建新运行目录和反馈记录，不覆盖本次：
# python scripts/run_s3_alpha.py run
python scripts/report_s3_alpha.py --run "{run}"
```

**阶段结论：{outcome}** 完整S0执行适配未完成这一事实保持；不以发现期筛选结果替代S4滚动选择测试或前向验证。
'''
    (run/'S3_alpha_report.md').write_text(report,encoding='utf8')
    prereg.write_json(run/'artifact_verification.json',dict(checks=checks,access_scope_verified=True,row_counts=row_counts,
        primary_complete_labels=valid_h12,stage_completed=True,passed_candidate_folds=len(passed),
        stage_conclusion='no_registered_alpha_pass' if passed.empty else 'provisional_discovery_passes_only'))
    manifest['report_source_sha256']=digest(Path(__file__))
    manifest['report_test_source_sha256']=digest(ROOT/'tests/test_s3_report.py')
    manifest['runtime']={k:importlib.metadata.version(k) for k in ('pandas','numpy','pyarrow','matplotlib')}
    prereg.write_json(run/'manifest.json',manifest)
    prereg.write_json(run/'artifact_hashes.json',{p.name:digest(p) for p in run.iterdir() if p.is_file() and p.name!='artifact_hashes.json'})
    prereg.write_json(ROOT/'artifacts/s3/latest_run.json',dict(path=str(run.relative_to(ROOT)),status='report_complete',report=str((run/'S3_alpha_report.md').relative_to(ROOT))))
    print(json.dumps(dict(stage_completed=True,passed_candidate_folds=len(passed),decisions=dict(decisions),artifact_checks=checks),ensure_ascii=False))


if __name__=='__main__':main()
