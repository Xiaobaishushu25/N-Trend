"""Audit and report V4 single-event structural prototypes, without account claims."""
import argparse
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
import explore_v4 as engine
import s2_preregister as reg
from factor_study_data import digest,specifications


def table(f):
    def fmt(v):
        if isinstance(v,(float,np.floating)):return '' if not np.isfinite(v) else f'{v:.3f}'
        return str(v)
    return '\n'.join(['|'+'|'.join(f.columns)+'|','|'+'|'.join(['---']*len(f.columns))+'|']+
        ['|'+'|'.join(fmt(v) for v in row)+'|' for row in f.itertuples(index=False,name=None)])


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path);args=p.parse_args();seal=engine.verify()
    run=args.run or ROOT/json.loads((engine.BASE/'latest_run.json').read_text(encoding='utf8'))['path']
    v=json.loads((run/'verification.json').read_text(encoding='utf8'));assert v['computation_complete'] and v['candidate_period_evaluations']==16
    events=pd.read_parquet(run/'events.parquet');controls=pd.read_parquet(run/'control_trades.parquet');summary=pd.read_csv(run/'summary.csv')
    full=json.loads((run/'summary.json').read_text(encoding='utf8'))
    assert len(events)==v['row_counts']['events'] and len(controls)==v['row_counts']['control_trades']
    assert events.event_id.is_unique and controls.event_id.is_unique
    checks=dict(invalid_complete_trades=0,invalid_projected_geometry=0,invalid_matches=0,cash_math_errors=0)
    specs=specifications(json.loads((ROOT/'scripts/factor-study-v1.json').read_text(encoding='utf8')))
    period_end={p['fold_id']:int(pd.Timestamp(p['discovery_end'],tz='Asia/Shanghai').value) for p in engine.PERIODS}
    for f in (events,controls):
        complete=f.loc[f.trade_status=='complete']
        valid=(complete.trade_entry_ns>=complete.signal_ns)&(complete.trade_exit_ns>=complete.trade_entry_ns)&(
            complete.trade_exit_ns<complete.fold_id.map(period_end))&(complete.trade_plan_risk_cny<=400+1e-8)
        checks['invalid_complete_trades']+=int((~valid).sum())
        mult=complete['product'].map({k[:-1]:s['multiplier'] for k,s in specs.items()})
        calculated=(complete.trade_exit-complete.trade_entry)*complete.side*mult-complete.trade_fees_cny
        checks['cash_math_errors']+=int((~np.isclose(calculated,complete.trade_net_cny,atol=1e-7)).sum())
        checks['cash_math_errors']+=int((~np.isclose(complete.trade_net_R,complete.trade_net_cny/(mult*complete.R_price),atol=1e-10)).sum())
        assert f.loc[f.trade_status!='complete','trade_net_R'].isna().all()
    e=events.set_index('event_id');c=controls.set_index('event_id')
    parent=c.parent_event_id.map(e.forecast_stop_R);other=c.parent_event_id.map(e.forecast_target_R)
    checks['invalid_projected_geometry']=int((~np.isclose(c.forecast_stop_R,parent,atol=1e-12)|~np.isclose(c.forecast_target_R,other,atol=1e-12)).sum())
    geometry=(c.target_price-c.stop)*c.side/c.R_price
    checks['invalid_projected_geometry']+=int((~np.isclose(geometry,c.forecast_stop_R+c.forecast_target_R,atol=1e-9)).sum())
    cols=['product','discovery_block','minute','side','signal_ns','activity_ratio','atr_pct_value','trade_net_R']
    ei=e[cols].add_prefix('e_');ci=c[cols+['parent_event_id']].add_prefix('c_')
    pairs=pq.ParquetFile(run/'matched_baseline.parquet');last=None;carry=set()
    for batch in pairs.iter_batches(batch_size=65536):
        f=batch.to_pandas().join(ei,on='event_id',validate='many_to_one').join(ci,on='control_id',validate='many_to_one')
        ok=f.event_id.eq(f.c_parent_event_id).to_numpy(copy=True)
        for k in ('product','discovery_block','minute','side'):ok&=f['e_'+k].to_numpy()==f['c_'+k].to_numpy()
        atr=f.e_atr_pct_value/f.c_atr_pct_value;activity=f.e_activity_ratio/f.c_activity_ratio
        ok&=atr.between(.67,1.5).to_numpy()&activity.between(.5,2).to_numpy()
        ok&=np.isclose(f.primary_delta_R,f.e_trade_net_R-f.c_trade_net_R,atol=1e-12)
        checks['invalid_matches']+=int((~ok).sum())
        checks['invalid_matches']+=int(((f.e_product==f.c_product)&(f.e_signal_ns==f.c_signal_ns)).sum())
        checks['invalid_matches']+=int(f.duplicated(['fold_id','candidate_id','replicate','c_product','c_signal_ns']).sum())
        keys=list(zip(f.fold_id,f.candidate_id,f.replicate));first,final=keys[0],keys[-1]
        if first==last:
            z=f.loc[(f.fold_id==first[0])&(f.candidate_id==first[1])&(f.replicate==first[2])]
            checks['invalid_matches']+=sum((p,int(t)) in carry for p,t in zip(z.c_product,z.c_signal_ns))
        if final!=last:carry=set()
        z=f.loc[(f.fold_id==final[0])&(f.candidate_id==final[1])&(f.replicate==final[2])]
        carry.update((p,int(t)) for p,t in zip(z.c_product,z.c_signal_ns));last=final
    assert not any(checks.values()),checks
    assert pairs.metadata.num_rows==v['row_counts']['matched_baseline']
    completed=events.loc[events.trade_status=='complete']
    trade_summary=completed.groupby(['fold_id','candidate_id']).agg(trades=('trade_net_cny','size'),
        mean_cash=('trade_net_cny','mean'),win_rate=('trade_net_cny',lambda x:float((x>0).mean())),
        worst_cash=('trade_net_cny','min'),losses_over600=('trade_actual_loss_over_600','sum'),
        ambiguous_exits=('trade_ambiguous','sum')).reset_index()
    trade_summary.to_csv(run/'single_event_diagnostics.csv',index=False)
    reasons=events.groupby(['fold_id','candidate_id','trade_status','trade_reason']).size().rename('count').reset_index()
    reasons.to_csv(run/'setup_disposition.csv',index=False)
    control_reasons=controls.groupby(['fold_id','trade_status','trade_reason']).size().rename('count').reset_index()
    control_reasons.to_csv(run/'control_projection_disposition.csv',index=False)
    cs=json.loads((engine.BASE/'candidate_manifest.json').read_text(encoding='utf8'))['candidates']
    compare=pd.DataFrame(index=[c['candidate_id'] for c in cs])
    for period in ('D1','D2'):
        f=summary.loc[summary.fold_id==period].set_index('candidate_id')
        for k in ('raw_setups','complete_primary','raw_date_equal_trade_R','signal_net_R','matched_fraction','ci_lower','ci_upper','holm_p','worst_trade_cny','decision'):
            compare[period+'_'+k]=f[k]
    for c in cs:
        compare.loc[c['candidate_id'],'state']=c['parameters']['peer_state']
        compare.loc[c['candidate_id'],'range_bars']=c['parameters']['range_bars']
        compare.loc[c['candidate_id'],'confirm_bars']=c['parameters']['confirmation_bars']
    compare.reset_index(names='candidate_id').to_csv(run/'candidate_comparison.csv',index=False)
    display=compare[['state','range_bars','confirm_bars']+[p+'_'+k for p in ('D1','D2') for k in ('raw_setups','complete_primary','raw_date_equal_trade_R')]].reset_index(names='candidate_id')
    plot=summary.pivot(index='candidate_id',columns='fold_id',values='raw_date_equal_trade_R')
    fig,ax=plt.subplots(figsize=(10,5));plot.plot.bar(ax=ax,color=['#286a8c','#d88832']);ax.axhline(0,color='black',lw=.8)
    ax.set_title('V4 | fixed structural exit, 1 lot, <=400 CNY planned risk');ax.set_ylabel('Net R per observed trade date');fig.tight_layout();fig.savefig(run/'prototype_net.png',dpi=160);plt.close(fig)
    audit=pd.read_csv(run/'data_access.csv');assert (audit.end_exclusive<='2025-11-01').all()
    first=summary.loc[(summary.fold_id=='D1')&(summary.candidate_id=='Q-C01')].iloc[0]
    second=summary.loc[(summary.fold_id=='D2')&(summary.candidate_id=='Q-C01')].iloc[0]
    losses=int((completed.trade_net_cny < -600).sum());worst=float(completed.trade_net_cny.min()) if len(completed) else None
    outcome='通过两期完整历史筛选：'+', '.join(v['pass_both_periods'])+'；仍非独立验证或资金部署许可。' if v['pass_both_periods'] else '无原型同时通过两期完整门槛，仍不进入资金组合部署。'
    runtime_note='冻结代码在部分非有限同组数据上触发 RuntimeWarning: invalid value encountered in scalar add；state_at的isfinite检查拒绝相应状态，未在看到结果后改代码/阈值。当前事件表的peer统计列是信号结束时快照，setup状态使用setup_ns前一根bar，不能将两者混同。'
    (run/'runtime_notes.md').write_text(runtime_note+'\n',encoding='utf8')
    report=f'''# V4 状态拆解与固定结构交易原型

**完成8个冻结原型×2个历史年度，共16次检验。{outcome}**

## 实际改进内容

本轮只改变三个事前登记因素：同组真正平静/方向抵消分歧、过去12/24根区间、立即/再等一根确认。没有新挑品种、没有优化止盈止损距离。

- 真正平静：其他品种日内位移/ATR的绝对值中位数<=1；方向分歧：位移中位数绝对值<=1，但绝对位移中位数>=2且MAD>=1。至少两个同组品种，目标品种剔除，同时间戳，不前填。
- 状态在假突破发生**之前**的一根5m收盘确定，要求与setup开始相邻；本地ER12<=.4，刺穿前12/24根区间至少.25前ATR后收回并反向确认。
- 多等一根时，只允许下一活跃bar继续收回且没有先碰目标或止损；失败取消，不反复等待。形态确认的原始警报全部记录，即使随后无法入场。
- 目标固定为setup前区间中点，止损固定为假突破极值加不利1 tick，最多12根或日盘收盘平仓。目标和止损在决策时已知，任何配置都不调退出参数。

V1–V3主要检验固定期限位移；V4另立固定结构收益合同，原始6/12/24位移仍保存为次要描述。**该改变不能把旧版S3失败改判为通过。** 协议、8模板、源代码在V4真实收益产生前冻结，UTC时间{seal['created_utc']}；原数据只读且哈希未变。

## 资金与成交口径

一手，初始计划风险预算400元（仍受600元上限），包括预计止损成交、双边手续费和不利滑点。下一实际5m开盘加不利1 tick入场，按该价格重新核算风险；超预算直接跳过，绝不收紧止损。目标无费用余量、跳空越过目标/止损、未知路径均保留原因，不补0收益。

同bar双触及先止损；后续开盘已经越过水平时先按可观察开盘判顺序，跳空止损用更差开盘减/加滑点。实际超600元亏损原样保留。本轮可评分原型事件中超过600元亏损记录数为 {losses}，最差单笔为 {worst} 元；这是本次参考撮合记录，不是未来亏损可保证条件。

**尚未模拟共享账户**：跨品种900元同时在险、保证金60%、日亏900元及组合回撤没有在本轮验证。单笔净额求和不是账户资金曲线，也不能视为5万元账户可实现收益率。真实涨跌停排队和滑点仍未由OHLC证实。

## 结果与机会数量

{table(display)}

较有样本的Q-C01（真正平静、12根区间、立即确认）：D1 {int(first.raw_setups)} 个警报、{int(first.complete_primary)} 个可评分事件，日期等权净结果约 {first.raw_date_equal_trade_R:+.3f}R；D2 {int(second.raw_setups)} 个警报、{int(second.complete_primary)} 个可评分事件，约 {second.raw_date_equal_trade_R:+.3f}R。R仍为信号ATR一手价值，不是600元。

延迟确认并未普遍改善结果，同时确认信号和可评分事件减少。分歧状态没有给出跨期稳定优势。少量配置正均值仍可能来自极少事件，完整样本/置信度门槛没有因此下调。

另一个待检验的状态定义问题是：当前用“日内累计位移/5分钟ATR”衡量平静，累计时长与分母期限不同，可能形成时段偏好。尚不能证明这是本轮不稳定的原因；后续更值得预注册同一盘中时点的历史波动标准化，再验证状态，而不是把已看到亏损的年份直接排除。本轮没有据此改阈值或重跑。

完整判断：{json.dumps(v['decisions'],ensure_ascii=False)}。本轮最大可评分样本数 {int(summary.complete_primary.max())}，基础门槛60；不能以少数盈利原型直接晋级。逐配置置信区间、Holm、匹配覆盖见summary.csv/candidate_comparison.csv，所有失败/稀疏配置保留。

![固定结构退出](prototype_net.png)

图中缺失柱表示没有可评分样本，并非0收益；样本数以表格为准。

## 机械对照与统计

对照同品种、4个月子段、5m时刻、方向，ATR/价格和成交活跃度采用V3事前容差。把事件**信号时**止损/目标的ATR距离投射至对照**信号时**价格，再独立模拟下一bar入场和同样400元一手预算，不使用事件的未来成交价或收益参数。它是匹配机械对照，不是一套当时使用未来事件参数的可部署策略。

投射不可评分/超预算样本完整写入control_trades.parquet，选择不依据盈利与否；只有可评分对照进入200次不重用匹配。1000次联合原始日期分块和8配置Holm；两期同一原型均通过完整条件才记本轮通过。这里与V3的原始位移主指标不同，不能直接比较两个版本的p值来宣称改进有效。

## 可复核交付

9项回归通过，覆盖前视、真正平静/抵消、下一bar确认、先到目标不补成交、一手预算、跳空亏损不截断、手算现金和信号时几何投射。C/JD/PB实际同步特征和全部8模板的前缀重算一致。

{runtime_note}

产物核验：{json.dumps(checks,ensure_ascii=False)}。events.parquet保留所有警报、原始标签和原型结果；control_pool为通用时点，control_trades为逐事件几何投射及所有跳过原因；matched_baseline为匹配关系。setup_disposition.csv列出每条未评分原因；single_event_diagnostics.csv列出单笔净额/胜率/最差亏损，仅作原型诊断。

两个历史区间仍为2023-11-01至2024-11-01、2024-11-01至2025-11-01。此前反馈已暴露，不能称独立未见；2025-11-01以后的新策略收益未读取，完整WFO次数未消耗。原始库、旧版本与失败记录不覆盖。

## 结论

{outcome} 主要障碍是机会数量少和跨期方向不稳定；不能据正的少数事件推断策略成立。状态拆解与固定结构退出已按预算完成，本轮不事后加过滤器或继续搜索退出参数。

复验：`python scripts/explore_v4.py verify`；`python -m unittest discover -s tests -p test_explore_v4.py -v`；报告重建：`python scripts/report_explore_v4.py --run "{run}"`。
'''
    (run/'exploration_report.md').write_text(report,encoding='utf8')
    reg.write_json(run/'artifact_verification.json',dict(checks=checks,complete_event_count=len(completed),observed_losses_over600=losses,
        observed_worst_event_cny=worst,shared_account_simulated=False,report_source_sha256=digest(Path(__file__))))
    reg.write_json(run/'artifact_hashes.json',{p.name:digest(p) for p in run.iterdir() if p.is_file() and p.name!='artifact_hashes.json'})
    reg.write_json(engine.BASE/'latest_run.json',dict(path=str(run.relative_to(ROOT)),status='report_complete',report=str((run/'exploration_report.md').relative_to(ROOT))))
    print(json.dumps(dict(completed=True,both_period_passes=v['pass_both_periods'],checks=checks,observed_worst_event_cny=worst,observed_losses_over600=losses),ensure_ascii=False))


if __name__=='__main__':main()
