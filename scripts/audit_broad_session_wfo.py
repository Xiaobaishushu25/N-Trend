"""Post-batch reconciliation and information-view attribution. Never replays."""
import collections
from pathlib import Path
import pandas as pd
import broad_session_wfo as w


def run():
    w.check();out=w.OUT
    for directory,file in [('training','manifest.json'),('evaluation','complete.json')]:
        for p,h in w.c.rd(out/directory/file)['files'].items():assert w.c.sha(out/directory/p)==h,p
    s=pd.read_csv(out/'selected_parameters.csv').fillna('');allc=[];fits=0
    for fold in range(1,9):
        folder=out/f'training/fold_{fold:02d}';d=pd.read_csv(folder/'all_candidates.csv');assert len(d)==1920;d['fold']=fold;allc.append(d)
        selection=w.c.rd(folder/'selection.json')
        for r in selection['selected']:
            row=d.loc[d.candidate_id==r['candidate_id']].iloc[0]
            assert row.score>0 and row.unknown==0
            assert r['model']['last_label_ns']<w.ns(selection['test'])
        for p in folder.glob('*_all_fits.json'):
            for record in w.c.rd(p):
                if record['model'] is not None:
                    fits+=1;assert record['model']['last_label_ns']<w.ns(record['validation_month']+'-01')
    cand=pd.concat(allc,ignore_index=True);assert len(cand)==15360
    summary=cand.groupby('view').agg(candidate_windows=('candidate_id','size'),positive_training_net=('net',lambda x:int((x>0).sum())),unknown_paths=('unknown',lambda x:int((x>0).sum())))
    complete_utility=cand.loc[(cand.score>0)&(cand.unknown==0)].groupby('view').size()
    summary['positive_complete_utility']=complete_utility.reindex(summary.index,fill_value=0)
    summary['selected_fold_rules']=s.groupby('view').size().reindex(summary.index,fill_value=0)
    risks={};trace=[]
    for label in ['base','stress']:
        tr=pd.read_csv(out/f'evaluation/{label}_trades.csv');eq=pd.read_csv(out/f'evaluation/{label}_equity.csv')
        joined=tr.merge(s[['fold','product','candidate_id','view']],left_on=['entry_fold','product','candidate_id'],right_on=['fold','product','candidate_id'],how='left',validate='many_to_one')
        byview=joined.groupby('view').net.agg(['count','sum']);summary[f'{label}_admitted_trades']=byview['count'].reindex(summary.index,fill_value=0)
        summary[f'{label}_admitted_net']=byview['sum'].reindex(summary.index,fill_value=0.)
        monthly=eq.set_index(pd.to_datetime(eq.time,utc=True).dt.tz_convert('Asia/Shanghai')).equity.resample('ME').last()
        monthly=monthly.reindex(pd.date_range('2024-08-31','2026-07-31',freq='ME',tz='Asia/Shanghai')).ffill()
        # Timestamp reindex normalized because source closes carry intraday time.
        monthly.index=monthly.index.strftime('%Y-%m');net=monthly.diff();net.iloc[0]=monthly.iloc[0]-50000
        pd.DataFrame({'equity':monthly,'net_yuan':net}).to_csv(out/f'{label}_monthly_equity.csv',index_label='month')
        for t in tr.loc[tr.net< -600].itertuples():
            raw=pd.read_pickle(out/'prepared/raw'/f'{t.contract}.pkl')
            live=raw.loc[(raw.start_ns>=t.entry_ns)&(raw.start_ns<=t.exit_ns)]
            live.to_csv(out/f'trace_{label}_{t.contract}_{t.entry_time[:10]}.csv',index=False)
            after_clock=live.loc[live.start_ns>=t.entry_ns+3*86400*10**9]
            trace.append(dict(scenario=label,contract=t.contract,product=t.product,entry_time=t.entry_time,exit_time=t.exit_time,side=t.side,
                entry=t.entry,exit=t.exit,stop=t.stop,planned_risk=t.planned_risk,net=t.net,reason=t.reason,quote_bars_during_hold=len(live),
                first_quote_after3day_clock=w.iso(int(after_clock.start_ns.iloc[0])) if len(after_clock) else None))
        risks[label]=dict(gross_before_slippage=float(tr.gross.sum()+tr.slippage_yuan.sum()),slippage=float(tr.slippage_yuan.sum()),fees=float(tr.fees.sum()),net=float(tr.net.sum()),
            max_observed_margin=float(eq.margin.max()),max_observed_planned_risk=float(eq.planned_risk.max()),actual_views=byview.to_dict('index'))
    summary.to_csv(out/'information_view_summary.csv',index_label='view')
    flags=collections.Counter(flag for x in s['flags'] for flag in str(x).split(';') if flag)
    w.c.js(out/'post_batch_audit.json',dict(candidate_windows_verified=len(cand),monthly_fits_verified=fits,selected_rules=len(s),flags=dict(flags),
        scenarios=risks,losses_beyond600=trace,no_new_backtests=True,all_fitted_labels_end_before_cutoff=True))
    text=(out/'REPORT.md').read_text(encoding='utf8');marker='## 结果解释与独立对账'
    if marker in text:text=text.split(marker)[0].rstrip()
    lines=['',marker,'',
        '以下只汇总已完成的冻结结果，不新增测试或更改参数。全部15,360候选窗口、23,040个月度拟合标签截止、训练选择封存哈希和测试文件哈希均已核验。','',
        '本轮共21个轮次规则入选：跨品种信息7个、持仓量/期限结构5个、全部信息6个、本地价格成交量3个。'
        '真实准入交易仅发生于第一轮：跨品种信息36笔净亏2684.40元，持仓量/期限结构26笔净亏1516.00元。'
        '本地和全部信息虽然在后续训练轮次入选，但账户此前已停机，没有账户成交验证；不能声称4类信息均被独立样本外否定。详情见information_view_summary.csv。','',
        '基准2024-10-16 09:05首次触发停机，后7轮0交易是永久停机后的持币，不是策略稳定或训练空选。'
        '压力情景2024-09-20 09:50更早停机；较高成本会提高计划风险、改变600/900元风控与保证金准入，因此两情景成交集不同。'
        '压力净亏4067.76元小于基准净亏4200.40元，不能解释为交易成本有利或压力测试更优。','',
        '压力情景最差PB2410多单：2024-09-12 09:05以16680入场，止损16580，含3跳与双倍费用的计划风险592.30元。'
        '仓位跨休市延至09-18 09:00，开盘16555，按3跳不利滑点以16540退出，净亏717.29元。'
        '3自然日时钟在休市中到期也无法形成成交；固定止损价与计划风险并不能限制跳空后的真实损失。trace_stress_SHFE.pb2410_2024-09-12.csv保存该路径。'
        '基准本次没有单笔亏损超过600元；未来仍可能超过600元和20%账户回撤。','',
        '基准同一批已成交交易：未计滑点方向毛额−535.00元，滑点3200.00元，费用465.40元，最终−4200.40元。'
        '该分解不额外重测零费用版本；本轮已准入交易即使不计摩擦仍未体现正毛优势。','',
        f"21个轮次选择中，低于30笔训练交易{flags.get('few_trades',0)}个，月份集中{flags.get('month_concentration',0)}个，参数尖峰{flags.get('parameter_peak',0)}个。"
        '离散网格邻域稳定不等于整个连续参数空间稳定；不能把相关模型拟合数视为独立证据。','',
        '训练月份分布按交易入场月份归属，正式各轮收益按共享账户权益变化计算，交易胜率/盈亏比按退出轮次的已平仓交易计算。'
        '信息覆盖和缺失比例见information_coverage.csv；没有用未来报价前填缺失。','',
        '补充命令python scripts/audit_broad_session_wfo.py仅复核账本、暴露、模型标签日期和既有成交归因。'
        '新行情模拟未启动，历史逐日真实费用/保证金与盘口成交认证仍未完成。']
    (out/'REPORT.md').write_text(text+'\n'+'\n'.join(lines)+'\n',encoding='utf8')
    w.c.js(out/'delivery_manifest.json',dict(files={str(p.relative_to(out)):w.c.sha(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name!='delivery_manifest.json'},
        auditor_sha256=w.c.sha(Path(__file__)),no_new_replay=True))
    print('Broad batch audit passed:',len(cand),'candidate windows;',fits,'purged monthly fits. No new backtest.')


if __name__=='__main__':run()
