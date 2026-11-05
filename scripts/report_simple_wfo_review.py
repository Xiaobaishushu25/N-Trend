"""Presentation-only supplement after the immutable batch; never runs backtests."""
import collections
import json
import pandas as pd
import simple_wfo as w


def run():
    out=w.OUT;rec=w.rd(out/'reconciliation.json');selections=[];counts=[]
    for fd in sorted((out/'training').glob('fold_*')):
        fold=w.rd(fd/'selection.json');c=pd.read_csv(fd/'all_candidates.csv')
        counts.append(dict(fold=fold['fold'],candidate_count=len(c),positive_validation_net=int((c.net>0).sum()),unknown_execution_candidates=int((c.unknown>0).sum()),selected=len(fold['selected'])))
        for r in fold['selected']:
            feat,lb,band,side,hold,stop=r['candidate']
            selections.append(dict(fold=fold['fold'],train_start=fold['train'],test_start=fold['test'],test_end=fold['end'],product=r['product'],candidate_id=r['candidate_id'],
                feature=feat,lookback=lb,band=band,side=side,hold=hold,stop_atr=stop,q25=r['cuts'][0],q75=r['cuts'][1],validation_net=r['validation_net'],flags=r['flags']))
    pd.DataFrame(selections).to_csv(out/'selected_parameters.csv',index=False)
    pd.DataFrame(counts).to_csv(out/'search_counts.csv',index=False)
    flag=collections.Counter(f for r in selections for f in r['flags'].split(';') if f)
    table=[];halt_info={}
    for scenario in ['base','stress']:
        summ=w.rd(out/f'evaluation/{scenario}_summary.json');eq=pd.read_csv(out/f'evaluation/{scenario}_equity.csv')
        halted=eq.loc[eq.halted]
        halt_info[scenario]=halted.iloc[0][['time','equity','peak','drawdown']].to_dict() if len(halted) else None
        for f in summ['folds']:
            table.append({k:f[k] for k in ['fold','train_start','test_start','test_end','start_equity','end_equity','net','max_drawdown','win_rate','payoff','profit_factor','trades']}|
                         dict(scenario=scenario,selected_products=','.join(f['selected_products']),status='halted_in_prior_fold' if f['fold']>1 and f['halted'] else 'halt_triggered' if f['halted'] else 'active'))
    pd.DataFrame(table).to_csv(out/'fold_summary.csv',index=False)
    w.js(out/'review_notes.json',dict(first_halt=halt_info,selected_window_rules=len(selections),selected_risk_flag_counts=dict(flag),no_backtest_reruns_after_results=True,
         caveat='later zero-return folds are enforced cash after drawdown stop, not independent strategy performance evidence'))
    report=out/'REPORT.md';text=report.read_text(encoding='utf8')
    first=halt_info['base'];b=rec['scenarios']['base'];s=rec['scenarios']['stress']
    lead=(f"**结果：本次未找到可确认的稳定正收益流程。** 基准净亏{abs(b['net']):,.2f}元（{b['net']/50000:.2%}），最终权益{b['final_equity']:,.2f}元；"
          f"压力情景净亏{abs(s['net']):,.2f}元，最终权益{s['final_equity']:,.2f}元。\n\n"
          f"**停机解释：** 基准在{first['time']}触发事先冻结的12%回撤停机规则；高水位{first['peak']:,.2f}元，权益{first['equity']:,.2f}元。"
          "第一轮共68笔，其余7轮因同一账户永久停开而没有成交。后7轮的0收益、0局部回撤是持币结果，不能当成策略稳定、策略空选或7次独立验证失败；账户累计最大回撤仍为12.12%。"
          "本次没有在看到亏损后放宽停机线、重置账户或重测后7轮。\n\n")
    text=text.replace('## 样本外共享账户\n\n','## 样本外共享账户\n\n'+lead)
    text=text.replace('基准最终权益：45379.22499999999 元；净额：-4620.775000000009 元；','基准最终权益：45,379.23 元；净额：-4,620.78 元；')
    text=text.replace('压力最终权益：44738.63 元；','压力最终权益：44,738.63 元；')
    extra=(f"\n\n基准胜率{b['win_rate']:.2%}，平均盈利/平均亏损{b['payoff']:.2f}，最差单笔{b['worst_trade']:.2f}元；"
           "本次没有超过600元的已成交单笔亏损，但合成跳空测试明确证明止损可以被突破。"
           f"最大记录在险{b['max_planned_risk']:.2f}元，最大5分钟收盘保证金占用{b['max_margin']:.2f}元。\n\n"
           "本次19个轮次选中规则全部有“再增加每边1跳后净额不正”的训练风险标记，其中17个有参数邻域中位净额不正、15个有正收益月份集中、12个月份中位净额不正。"
           "这些是训练阶段已经保存的信息，说明所选只是当前排序中相对靠前的薄弱候选，并非稳定赢家。没有在看到测试亏损后把风险标记改为新门槛。\n\n"
           "基准未滑点方向毛额为-565.00元，双边滑点成本3,720.00元，手续费及佣金335.78元，合计净亏4,620.78元。"
           "滑点是主要损耗，但去掉全部摩擦后的已准入交易仍为负；该拆分不是对零费用新策略的重测。")
    text=text.replace('\n## 参数及训练选择',extra+'\n\n## 参数及训练选择')
    text=text.replace('单价或零成交量bar不准入，退出延至可执行bar；',
        '前一已完成bar为单价或零成交量时不提交入场；当前入场bar锁住或无效则标作无法确定成交，训练中不可选、测试中不得填零收益。已有持仓退出延至可执行bar；')
    text=text.replace('14:55开盘平仓，防止假设15:00收盘市价必成交。',
        '计划14:55开盘平仓，无法确定可成交时继续占用仓位并尝试下一实际开盘，可能被动隔夜；本次实际成交没有隔夜记录。不能用日盘bar声称已经核验夜间浮亏。')
    text=text.replace('每次含6个月度扩展窗校准/验证，不作为独立试验。',
        '每次含6个月度扩展窗校准/验证，共414,720个候选月度评分单元、11,520次扩展窗分位数拟合、19次最终阈值重估，不能当成独立试验。')
    text=text.replace('无失败候选删除。',
        '无失败候选删除。共589次候选窗口净额为正；3,063次含未知成交/未平仓路径，其中未知路径后的收益不再计数，故不能把这些部分净额当作完整训练净额或参与选择。')
    text=text.replace('真实累计次数未知。','真实累计次数未知。数据实际有22个品种，每轮训练覆盖度并列时按字母排序取20个，因此本次未搜索UR/V；不是依据未来收益剔除。')
    text += ('\n\n## 交付索引与核验\n\n'
             '- fold_summary.csv：两种情景每轮共享账户净收益、回撤、胜率、盈亏比、交易数和停机状态。\n'
             '- selected_parameters.csv：全部19个轮次选择的信号、持仓/止损参数、绝对阈值及风险标记。\n'
             '- search_counts.csv 与 training/fold_XX/all_candidates.csv：全部候选计数、败例、月度验证、邻域与费用敏感性。\n'
             '- evaluation/base_trades.csv、base_equity.csv：已准入交易和连续样本外账户曲线；stress_*为预先固定压力情景。\n'
             '- base_oos_monthly.csv、stress_oos_monthly.csv：完整24个月的净收益，含停机后的零月份。\n'
             '- reconciliation.json、causality_audit.json、execution_tests.txt：账本与手续费重算、前后仓位不重叠、风险约束、因果日期及10项合成测试均通过。\n'
             '- protocol.json、freeze.json、frozen_simple_wfo.py：正式评估前冻结的规则、代码和输入哈希。\n'
             '- paper_forward.json：冻结后的前向模拟约定，状态NOT_STARTED。本次没有可批准实盘的正收益候选。\n\n'
             '补充复现：基础report阶段后运行python scripts/audit_simple_wfo.py，再运行python scripts/report_simple_wfo_review.py。'
             '报告脚本仅展示已完成结果，不进行额外参数搜索或回测。stress_trades.csv中的margin_rate是原始规格基础字段0.20；'
             '压力账户实际占用按冻结协议0.25计算，以stress_equity.csv和协议为准。\n')
    report.write_text(text,encoding='utf8')
    w.js(out/'delivery_manifest.json',dict(files={str(p.relative_to(out)):w.sha(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name!='delivery_manifest.json'},
        added_code={str(p.relative_to(w.ROOT)):w.sha(p) for p in [w.ROOT/'scripts/audit_simple_wfo.py',w.ROOT/'scripts/report_simple_wfo_review.py']},purpose='presentation and reconciliation only; frozen search/evaluation unchanged'))
    print('Review supplements and delivery hashes saved; no evaluation rerun.')


if __name__=='__main__':run()
