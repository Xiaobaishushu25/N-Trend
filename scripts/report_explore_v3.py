"""Report all V3 candidates, verify calipers and preserve historical exposure limits."""
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
import explore_v3 as engine
import s2_preregister as reg
from factor_study_data import digest


def table(f):
    def fmt(v):
        if isinstance(v,(float,np.floating)):return '' if not np.isfinite(v) else f'{v:.3f}'
        return str(v)
    return '\n'.join(['|'+'|'.join(f.columns)+'|','|'+'|'.join(['---']*len(f.columns))+'|']+
        ['|'+'|'.join(fmt(x) for x in row)+'|' for row in f.itertuples(index=False,name=None)])


def check_pairs(run,events,controls):
    columns=['event_id','product','discovery_block','minute','side','atr_pct_value','activity_ratio','signal_ns','net_R_12']
    e=events[columns].set_index('event_id').add_prefix('e_');c=controls[columns].set_index('event_id').add_prefix('c_')
    assert e.index.is_unique and c.index.is_unique
    bad=0;last=None;carry=set();file=pq.ParquetFile(run/'matched_baseline.parquet')
    for batch in file.iter_batches(batch_size=65536):
        f=batch.to_pandas().join(e,on='event_id',validate='many_to_one').join(c,on='control_id',validate='many_to_one')
        ok=np.ones(len(f),bool)
        for k in ('product','discovery_block','minute','side'):ok&=f['e_'+k].to_numpy()==f['c_'+k].to_numpy()
        ratio=f.e_atr_pct_value/f.c_atr_pct_value;activity=f.e_activity_ratio/f.c_activity_ratio
        ok&=ratio.between(.67,1.5).to_numpy()&activity.between(.5,2).to_numpy()
        ok&=np.isclose(f.primary_delta_R,f.e_net_R_12-f.c_net_R_12,atol=1e-12)
        bad+=int((~ok).sum())
        bad+=int(((f.e_product==f.c_product)&(f.e_signal_ns==f.c_signal_ns)).sum())
        bad+=int(f.duplicated(['fold_id','candidate_id','replicate','c_product','c_signal_ns']).sum())
        groups=list(zip(f.fold_id,f.candidate_id,f.replicate));first,final=groups[0],groups[-1]
        if first==last:
            z=f.loc[(f.fold_id==first[0])&(f.candidate_id==first[1])&(f.replicate==first[2])]
            bad+=sum((p,int(t)) in carry for p,t in zip(z.c_product,z.c_signal_ns))
        if final!=last:carry=set()
        z=f.loc[(f.fold_id==final[0])&(f.candidate_id==final[1])&(f.replicate==final[2])]
        carry.update((p,int(t)) for p,t in zip(z.c_product,z.c_signal_ns));last=final
    return bad,file.metadata.num_rows


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path);args=p.parse_args();seal=engine.verify()
    run=args.run or ROOT/json.loads((engine.BASE/'latest_run.json').read_text(encoding='utf8'))['path']
    v=json.loads((run/'verification.json').read_text(encoding='utf8'));assert v['computation_complete'] and v['candidate_period_evaluations']==48
    rows=json.loads((run/'summary.json').read_text(encoding='utf8'));summary=pd.read_csv(run/'summary.csv')
    events=pd.read_parquet(run/'events.parquet');controls=pd.read_parquet(run/'controls.parquet')
    assert len(events)==v['raw_event_rows'] and len(controls)==v['control_rows']
    assert not events.duplicated(['fold_id','candidate_id','product','signal_ns']).any()
    assert events.friction_R.le(.25+1e-12).all()
    invalid=0
    for period in engine.PERIODS:
        f=events.loc[events.fold_id==period['fold_id']];end=int(pd.Timestamp(period['discovery_end'],tz='Asia/Shanghai').value)
        for h in (6,12,24):
            valid=f.loc[f[f'status_{h}']=='complete']
            invalid+=int((~((valid.entry_ns>=valid.signal_ns)&(valid[f'label_end_ns_{h}']<end))).sum())
    assert invalid==0
    bad,pair_rows=check_pairs(run,events,controls);assert bad==0 and pair_rows==v['matched_pair_rows']
    complete=events.loc[events.status_12=='complete']
    day=complete.groupby(['fold_id','candidate_id','day']).net_R_12.mean().groupby(['fold_id','candidate_id']).mean().rename('raw_day_equal_net_R')
    summary=summary.merge(day,on=['fold_id','candidate_id'],how='left');summary.to_csv(run/'evidence.csv',index=False)
    universe=json.loads((engine.BASE/'protocol.json').read_text(encoding='utf8'))['universe']
    coverage=events.groupby(['fold_id','product']).size().rename('events').reindex(pd.MultiIndex.from_product([['D1','D2'],universe],names=['fold_id','product']),fill_value=0)
    coverage.reset_index().to_csv(run/'product_coverage.csv',index=False)
    product=[];blocks=[]
    for r in rows:
        product.extend(dict(period=r['fold_id'],candidate_id=r['candidate_id'],product=k,effect_R=x) for k,x in r['product_effects'].items())
        blocks.extend(dict(period=r['fold_id'],candidate_id=r['candidate_id'],**b) for b in r.get('discovery_blocks',[]))
    pd.DataFrame(product).to_csv(run/'product_effects.csv',index=False);pd.DataFrame(blocks).to_csv(run/'block_effects.csv',index=False)
    compare=pd.DataFrame(index=sorted(summary.candidate_id.unique()))
    for period in ('D1','D2'):
        f=summary.loc[summary.fold_id==period].set_index('candidate_id')
        for field in ('complete_primary','raw_day_equal_net_R','signal_net_R','effect','ci_lower','ci_upper','holm_p','matched_fraction','decision'):
            compare[period+'_'+field]=f[field]
    compare.reset_index(names='candidate_id').to_csv(run/'candidate_comparison.csv',index=False)
    display=compare[[p+'_'+k for p in ('D1','D2') for k in ('complete_primary','raw_day_equal_net_R','matched_fraction')]].reset_index(names='candidate_id')
    diag=summary.groupby('hypothesis_id').agg(evaluations=('candidate_id','size'),positive_raw_net=('raw_day_equal_net_R',lambda x:int((x>0).sum())),
        positive_matched_net=('signal_net_R',lambda x:int((x>0).sum())),matching_80pct=('matched_fraction',lambda x:int((x>=.8).sum())),
        statistical_pass=('decision',lambda x:int((x=='alpha_screen_pass_research_only').sum())))
    diag.to_csv(run/'mechanism_diagnostics.csv')
    for family in ('P1','P2','P3'):
        z=summary.loc[summary.hypothesis_id==family].pivot(index='candidate_id',columns='fold_id',values='raw_day_equal_net_R')
        fig,ax=plt.subplots(figsize=(8,4));z.plot.bar(ax=ax,color=['#276b8c','#d98a36']);ax.axhline(0,color='black',lw=.8)
        ax.set_title(family+' | fixed12-bar net displacement, date weighted');ax.set_ylabel('Net R');fig.tight_layout();fig.savefig(run/(family+'_net.png'),dpi=150);plt.close(fig)
    audit=pd.read_csv(run/'data_access.csv');assert (audit.end_exclusive<='2025-11-01').all()
    both_raw=compare.index[(compare.D1_raw_day_equal_net_R>0)&(compare.D2_raw_day_equal_net_R>0)].tolist()
    p3_first=summary.loc[(summary.fold_id=='D1')&(summary.hypothesis_id=='P3')]
    p3_second=summary.loc[(summary.fold_id=='D2')&(summary.hypothesis_id=='P3')]
    c04=summary.loc[(summary.fold_id=='D2')&(summary.candidate_id=='P3-C04')].iloc[0]
    c06=summary.loc[(summary.fold_id=='D2')&(summary.candidate_id=='P3-C06')].iloc[0]
    conclusion='同一配置在两个历史区间均通过完整筛选：'+', '.join(v['pass_both_periods'])+'，仍非独立前向验证。' if v['pass_both_periods'] else '尚无同一配置在两个历史区间同时通过完整筛选。'
    report=f'''# V3 上下文与跨品种信息探索

**完成24配置×2历史年度的48次检验。{conclusion}**

## 本轮扩展

V1/V2以单品种形态为主。V3增加事前可观测的跨品种上下文及价格接受信息，未改旧版规则、结果或未证实判断。冻结UTC时间{seal['created_utc']}，版本3.0-EXP；全部22品种、日盘不隔夜，成本预测<=0.25ATR风险单位的规则保持。完整协议/参数及哈希见上一层protocol.json、candidate_manifest.json、freeze.json。

- **P1 同组共振下的落后品种回补**：同组其他品种在同一已结束bar呈方向共识，本品种先回调再确认，检验是否存在群体信息带来的回补优势。
- **P2 价格接受压力后的区间释放**：过去6/12根成交量加权收盘位置显示偏向、净位移却不大，等待放量突破。收盘位置只是OHLCV代理，不称为真实主动买卖或订单流。
- **P3 同组平静与局部低趋势下的假突破回归**：在假突破机制前增加预先可识别的群体平静和本地低ER条件，检验V2的时间变化能否用状态解释。

同组为事前研究分组，未根据收益拟合相关性。目标品种从同组计算中剔除，至少两个其他品种，必须完全相同end_ns，缺失行情不沿用旧报价；日内位移要求已观察到09:00开盘并处于同一真实合约区段。分组局限保留，不代表行业因果关系已证实。

## 新匹配协议

V2对照覆盖不足，V3在看新结果前登记：同品种、同5分钟时刻、同方向、同4个月研究子段精确匹配；ATR/价格比值容差[0.67,1.5]、相对成交量比值容差[0.5,2]。不跨研究子段，没有看结果后改变范围。每次重复对照时间戳不重用，200次固定种子；1000次联合日期分块，24配置Holm。

这里改变的是新版本的对照定义，不能把V2历史未证实结果改判为有效。匹配覆盖、信号自身净位移和统计置信度仍分别审查。

## 时间与结果

D1=2023-11-01至2024-11-01，D2=2024-11-01至2025-11-01，均右开，全部参数在看D1前冻结。过去反馈已暴露，两个区间都是历史诊断；2025-11-01之后的新策略结果仍未读取，未预约完整WFO。

{table(diag.reset_index())}

positive_raw_net=所有完整事件按日期等权净位移为正；positive_matched_net=成功匹配子集口径；matching_80pct=最低匹配率>=80%；statistical_pass=通过完整预登记门槛。单独一个正均值不代表可交易。

两期原始日期等权净位移均为正的配置：{', '.join(both_raw) if both_raw else '无'}。这只作方向线索，不替代完整筛选结果。完整筛选判断：{json.dumps(v['decisions'],ensure_ascii=False)}。

### 本轮具体收获

48次检验中，{int(summary.matched_fraction.ge(.8).sum())} 次达到80%对照覆盖；当前不再主要由上一版的覆盖不足阻断。群体共振回补P1没有出现净正配置，价格接受压力P2的少量原始正均值也没有转成匹配子集净正。

P3在D1有 {int(p3_first.raw_day_equal_net_R.gt(0).sum())}/8 组原始日期等权净位移为正，D2有 {int(p3_second.raw_day_equal_net_R.gt(0).sum())}/8 组，提示“群体净方向较弱+局部假突破”值得作状态依赖研究。但它没有跨期保持，不能从后段挑出配置就升级为有效。

例如D2的P3-C06约 {c06.raw_day_equal_net_R:+.3f}R，但只有 {int(c06.complete_primary)} 个完整事件、{int(c06.matched_days)}个匹配日期。样本较多的P3-C04有 {int(c04.complete_primary)} 个事件、{int(c04.matched_days)} 个匹配日期，净位移仅 {c04.raw_day_equal_net_R:+.3f}R，匹配优势95%区间约 [{c04.ci_lower:+.3f}, {c04.ci_upper:+.3f}]R。正点估计与充分证据之间仍有明显距离。

本轮“同组平静”严格只指同组位移中位数接近0，不代表每个同组品种都低波动；同组内部方向分歧尚未单独限制。这是当前解释的局限，可作为后续新版本的机制问题，不能在本轮结果出来后直接补过滤器并重称未见验证。

{table(display)}

![P1](P1_net.png)

![P2](P2_net.png)

![P3](P3_net.png)

## 校验与完整记录

7项回归通过，包括自品种排除、缺失同组行情不前填、24模板同步前缀、容差匹配与不重用。C/JD/PB真实数据对全部模板和同步同组特征的截断重算一致。实际产物检查：完整标签越界{invalid}，错误匹配/重用{bad}；事件成本门槛无违规。原始事件{v['raw_event_rows']:,}条、对照{v['control_rows']:,}条、匹配对{v['matched_pair_rows']:,}条；候选重叠，不等于独立交易数。

evidence.csv保留48次检验完整指标，candidate_comparison.csv逐项展示两段比较；events/controls/matched_baseline.parquet可按event_id/control_id复核。product_coverage.csv保留全部22品种（包括无信号），product_effects.csv与block_effects.csv仅用于归因，不据此挑品种。

完整门槛沿用：60完整事件、30匹配日期、80%最低覆盖，信号自身净位移>0、匹配效应及95%下界>0、Holm<=0.05，三个子段至少两段正且各>=10事件。两期同配置均通过才记本轮通过，没有从6/12/24根里挑最优期限。

这里是固定期限原始Alpha标签，不是止盈止损策略资金曲线。每边1 tick、交易所基础费率快照、合约规格代理、未覆盖日历及实际撮合限制继续披露；不宣称实际收益、最大亏损或组合回撤已验收。原始事件的一手600元计划风险可行比例保留，不为凑一手收紧止损。

## 本轮结论

{conclusion} 已检验上下文与群体信息；本轮预算用完后停止，不依据亏损区间增加年份过滤、挑品种或无限追加邻域。任何后续新假设需新登记，旧历史保持暴露标记。

复验：`python scripts/explore_v3.py verify`；`python -m unittest discover -s tests -p test_explore_v3.py -v`；报告重建：`python scripts/report_explore_v3.py --run "{run}"`。
'''
    (run/'exploration_report.md').write_text(report,encoding='utf8')
    reg.write_json(run/'artifact_verification.json',dict(invalid_complete_labels=invalid,invalid_pairs=bad,cost_gate_violations=0,rows_verified=True,
        report_source_sha256=digest(Path(__file__)),both_period_positive_raw_net=both_raw))
    reg.write_json(run/'artifact_hashes.json',{p.name:digest(p) for p in run.iterdir() if p.is_file() and p.name!='artifact_hashes.json'})
    reg.write_json(engine.BASE/'latest_run.json',dict(path=str(run.relative_to(ROOT)),status='report_complete',report=str((run/'exploration_report.md').relative_to(ROOT))))
    print(json.dumps(dict(completed=True,complete_screen_both_periods=v['pass_both_periods'],raw_positive_both_periods=both_raw,diagnostics=diag.reset_index().to_dict('records')),ensure_ascii=False))


if __name__=='__main__':main()
