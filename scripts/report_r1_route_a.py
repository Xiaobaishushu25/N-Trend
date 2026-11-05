"""Summarize the two frozen route A rules without selecting variants."""
import json
import shutil
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import r1_route_a as r
from report_r1_diagnostics import table


def main():
    out=r.BASE/'development'
    q=r.pd.read_csv(out/'candidate_comparison.csv');tr=r.pd.read_csv(out/'trades.csv')
    ds=r.pd.read_csv(out/'setup_dispositions.csv');fun=r.pd.read_csv(out/'opportunity_funnel.csv')
    v=json.loads((out/'verification.json').read_text(encoding='utf8'))
    freeze=json.loads((r.BASE/'freeze.json').read_text(encoding='utf8'))
    complete=tr.loc[tr.status=='complete'].copy()
    complete['month']=complete.day.str[:7]
    for label,keys in [('by_product',['period','candidate_id','slip_ticks','product']),('by_month',['period','candidate_id','slip_ticks','month'])]:
        values=[]
        for key,g in complete.groupby(keys):
            row=dict(zip(keys,key));row.update(trades=len(g),active_dates=g.day.nunique(),gross_yuan=g.gross_yuan.mean(),
                fees_yuan=g.fees_yuan.mean(),slippage_yuan=g.slippage_yuan.mean(),net_yuan=g.net_yuan.mean(),
                net_yuan_date_equal=g.groupby('day').net_yuan.mean().mean(),net_sum_yuan=g.net_yuan.sum())
            values.append(row)
        r.pd.DataFrame(values).to_csv(out/(label+'.csv'),index=False)
    # All rejected-product cells remain in the funnel; an absent accepted trade isn't a zero-P&L trade.
    ds.groupby(['period','candidate_id','slip_ticks','product','reason'],dropna=False).size().rename('events').reset_index().to_csv(out/'by_product_dispositions.csv',index=False)
    funnel=[]
    for keys,g in ds.groupby(['period','candidate_id','slip_ticks']):
        row=dict(zip(['period','candidate_id','slip_ticks'],keys));row.update(structures=len(g),
            confirmation_failed=int(g.reason.isin(['confirmation_not_observed','confirmation_gap_day_or_invalid','confirmation_failed']).sum()),
            insufficient_scheduled_time=int(g.reason.eq('insufficient_scheduled_holding_time').sum()),
            lot_budget_rejected=int(g.reason.eq('one_lot_budget').sum()),reserved_slot_rejected=int(g.reason.eq('reserved_nonoverlap_slot').sum()),
            accepted=int(g.status.isin(['complete','unknown']).sum()))
        funnel.append(row)
    funnel=r.pd.DataFrame(funnel);funnel.to_csv(out/'funnel_summary.csv',index=False)
    economics_pass=[d['candidate_id'] for d in v['decisions'] if d['economic_gate_pass']]
    md='# R1 路线 A：30m/60m 固定规则开发诊断\n\n'
    if not economics_pass:
        md+='**两条冻结规则均未通过开发经济门槛。本轮停止，不访问保留期、不继续改参数。当前没有可部署策略。**\n\n'
    else:
        md+=f'经济门槛通过：{economics_pass}。执行资料仍未齐备，不运行共享账户认证或保留期验收。\n\n'
    md+='这次实际执行 2 个新模板×D1/D2，共 4 次开发评估；1/2 tick 成交情景逐项记录，没有按收益择优。累计已知 82 个模板、260 次配置×窗口评估；R1 前轮的描述性诊断单独登记，更早反馈仍未知。D1/D2 已暴露，不能称独立样本外。\n\n'
    md+='## 冻结规则\n\n'
    md+='A30/A60 仅高周期不同：两个最新完整时钟桶必须在同一真实合约连续历史段。高点、低点、收盘同时上升，且当前桶阳线，构成长结构；完全反向为空。随后仅第一根紧邻的完整 5m 可确认，须同向实体且收盘进一步越过结构收盘；失败即取消。确认后下一实际 bar 开盘加不利滑点入场。\n\n'
    md+='前一个高周期桶可以来自前一日，历史隔夜变化属于已知信息；持仓仍严格日盘。没有要求“同日两个60m桶”，因为完整时钟60m桶主要为09–10和14–15，同日第二个桶后已经无法开仓。这个时钟结构限制未通过跨休息拼接或跨夜持仓绕过。\n\n'
    md+='止损固定为最新结构的反向极值外1tick；不设止盈或移动止损。固定持有最多24个活跃5m bar（120活跃分钟），日盘结束强平；信号时根据已知时段表，剩余不足24根直接拒绝。午休等休市不计入活跃分钟，因此实际墙钟持有时间可能超过两小时。缺失路径是 unknown，不假设跨缺口成交。\n\n'
    md+='一手计划风险预算400元，上限600元；超过400直接跳过，不收紧技术止损。原 stop 价位不因压力滑点变化，2tick情景从入场预算开始独立撮合。每品种/模板仅一个24bar计划占用槽，早止损后仍占满该计划槽，消除结果驱动的再入场；两模板分开诊断，不组合。\n\n'
    md+='## 收益、成本和证据规模\n\n'
    md+=table(q,['period','candidate_id','slip_ticks','complete_trades','active_dates','unknown_paths','gross_yuan','slippage_yuan','fees_yuan','net_yuan','net_yuan_date_equal','ci_lower','ci_upper'])
    md+='\n`gross_yuan` 为未加滑点的下一开盘至实际退出基础价的方向现金位移，`net=gross-slippage-fees`。单笔普通均值和“先日期内平均、再日期等权”的结果分列。活跃日期置信区间采用同一来源日期联合所有品种的5日移动块抽样1000次，仅作开发不确定性描述，不是资金日收益区间。没有交易的日期不作为零单笔填入。\n\n'
    md+='完整的逐品种/月结果均已输出，不事后删品种。`daily_closed_pnl.csv` 只是独立事件的平仓现金归属，未统一资金占用，不能当作5万元账户资金曲线或据此报告账户回撤。\n\n'
    md+='## 机会损耗\n\n'+table(funnel,list(funnel.columns))
    md+='\n每个结构仅一次确认，不追加第六种过滤。所有拒绝原因和时刻在 setup_dispositions.csv，逐品种无成交格子也保留在 by_product_dispositions.csv。高周期波幅较大并不保证一手止损容得下，日内剩余时间与400元预算均是经济可行性的一部分。\n\n'
    md+='## 同时刻机械对照\n\n'
    md+=table(q,['period','candidate_id','slip_ticks','complete_trades','paired_events','paired_effect_date_equal','matched_signal_net_date_equal'])
    md+='\n对照在同一信号时刻分别做多/空，复制信号时已知的止损距离，再取两边结果平均；只比较两边都可评分的配对子集。没有用未来实际入场价设置对照。这里“相对更好”仍须与策略自身盈利分开判断，不是独立随机实验或匹配无偏证明。\n\n'
    md+='## 晋级决策\n\n'+table(r.pd.DataFrame(v['decisions']),['candidate_id','economic_gate_pass','execution_readiness','decision'])
    md+='\n预注册经济门槛同时要求：D1/D2 每段至少100笔、60活跃日期、日期等权净值为正、2tick压力仍正、主情景95%下界为正、相对对照优势为正且配对覆盖≥80%，无已接受未知路径。该门槛没有在看收益后改动。即使经济门槛通过，BU/JD/SH规格、历史费率/保证金/日历、主力映射可得性以及共享账户集成仍需先解决，不能直接消费保留期。\n\n'
    if not economics_pass:
        md+='**下一步：停止这一批简单日内方向结构假设。** 不把60m改成45m、不增加回调/止盈倍数，不因结果调高风险预算或取消滑点。若继续寻找新机制，应另立可检验的市场结构问题与搜索预算；本轮没有足够证据支持进入正式资金组合或独立前向模拟。这里未通过的是这两个具体冻结规则，不能推出所有30m/60m策略均不可能有效。\n\n'
    md+='## 执行边界与复验\n\n'
    md+='本轮是一手 OHLC execution_proxy，没有实盘口点差、排队、涨跌停可成交证据。费用沿用2026-10-22交易所固定快照，保留开平今昨，不含期货公司加收。BU/JD/SH规格覆盖仍待有日期证据；保证金未知。本轮未生成共享账户回撤，也未宣称达到5万元/20%回撤、同时在险900元和保证金60%的账户目标。\n\n'
    worst=complete.groupby(['candidate_id','slip_ticks']).net_yuan.min().reset_index(name='worst_actual_net_yuan')
    md+='实际亏损未按600元截断，逐事件 gap_loss 如实入账：\n\n'+table(worst,list(worst.columns))
    md+=f"\n代码/输入/规则在计算前冻结；git HEAD `{freeze['git_sha']}`；主库 SHA256 `{freeze['data_sha256']}`。\n\n"
    md+='六组合成回归、C/JD/PB×D1/D2共六组从SQL重读的前缀检查、现金恒等式和数据日期边界通过。`python scripts/r1_route_a.py verify` 为只读复验；`python -m unittest discover -s tests -p test_r1_route_a.py -v` 为合成回归。不要重复 run 来增加独立证据，目录拒绝覆盖。\n'
    (out/'R1_route_A_report.md').write_text(md,encoding='utf8')
    shutil.copy2(Path(__file__),out/'report_source.py.txt')
    print('Route A report written')


if __name__=='__main__':main()
