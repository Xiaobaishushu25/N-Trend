"""Human-readable findings and coverage limits from saved matched executions."""
import sys
sys.dont_write_bytecode=True
import collections
import statistics as st
import research_swing_jd_reclaim_matched_execution as m
from report_swing_jd_reclaim_matched_execution import cash


def run():
    summary=m.R(m.RUN/'summary.json');pairs=m.R(m.RUN/'paired-results.json')
    matches=[];executions={}
    for per in ['early','recent','validation2025']:
        matches+=m.R(m.RUN/per/'matches.json')
        for z in m.R(m.RUN/per/'fee-priced-decisions.json'):
            executions[z['model']+'/'+z['suffix'],z['row']['event_key']]=z['row']
    qualified=[q for q in matches if q['status']=='matched']
    fill_diagnostics={}
    for y in ['2025','2026']:
        for model in ['BASE','F2S8']:
            case=model+'/corrected_touch/slip1';qs=[q for q in qualified if q['year']==y]
            real=[executions[case,q['key']] for q in qs]
            hosts=[executions[case,h['key']] for q in qs for h in q['hosts']]
            def stats(rs):
                filled=[r for r in rs if m.a.routing.EV(r)['status']=='closed']
                return dict(decisions=len(rs),closed=len(filled),
                    mean_closed_netR=st.mean(cash(r) for r in filled) if filled else None,
                    statuses=dict(collections.Counter(m.a.routing.EV(r)['status'] for r in rs)))
            fill_diagnostics[y+'/'+model]=dict(real=stats(real),hosts=stats(hosts))
    m.write('fill-diagnostics.json',fill_diagnostics)
    lines=['# 鸡蛋收回：匹配背景与原生成交对照', '',
        '日期：2026-10-27（北京时间）。结论：本次严格可匹配的小样本未显示稳定的扣费增量，不能把收回结构认定为鸡蛋的稳定优势。覆盖明显不足，尤其2025仅匹配到9月，不能把此结果外推到全部42个信号。', '',
        '## 本轮具体做了什么', '',
        '从上一轮全部42个收回决策出发，先冻结匹配协议，再生成普通时点对照。真实信号与对照均通过同一个冻结原生执行器，测试BASE原退出和F2S8退出；每套均有正常/额外延迟一分钟入场×每次成交1—4tick滑点，共8种执行场景。未知仓位政策不影响本次独立订单比较，因此不把8种扩写成16种。', '',
        '匹配必须同实际合约、同月份、同15分钟时刻、过去8柱动量符号一致，ATR14比例在0.5—2之间。过去21柱须有完整正成交量分钟、且未换月；对照排除距任意真实收回信号前后32根交易柱的时点。每个信号至少3个候选，不足不放宽。此排除距离为本轮事先固定，与旧16柱ATR诊断不同，覆盖数量不能直接比较为策略退化。', '',
        '对照保留该真实信号的方向、点数风险、计划2R、S0与入场价距离、原A结构资格及净实体资格；所有价格锚点平移到对照收盘，时间锚点等距移动，使用对照真实预警柱。它没有重新声称该时点存在ABC，检验的是“给定这份交易计划，收回时点是否更有价值”。这不是一套可部署的普通时点交易策略。', '',
        '匹配只看时点之前的价格背景，不根据未来盈利或完整窗口筛选候选。匹配日可以位于真实信号之后，排除区也使用整段信号集合，因此整体属于回顾性机制对照，不能作为可实盘运行的历史选样程序。', '',
        '## 覆盖率与缺口', '',
        '|时间段|全部信号|至少3对照的信号|控制订单计划|覆盖说明|',
        '|---|---:|---:|---:|---|',
        '|2025年9—12月|10|3|11|3个全部在9月；10、11月不满足严格匹配，12月无信号|',
        '|2026开发覆盖|32|6|23|1月2个、3月1个、4月2个、6月1个；后段11个均不足3对照|', '',
        '合计9/42信号、34个控制计划。2026匹配子集中5个空头、1个多头；2025为2个空头、1个多头。样本不仅少，还不代表完整月份与方向分布。未匹配的33个信号均保留原因和可用候选数，没有删除。', '',
        '## 主执行场景：扣费后的配对增量', '',
        '下面均按计划现金风险归一为R，每个真实信号等权，先平均它自己的所有对照，再平均信号间差额。每次成交1tick、正常次分钟开盘入场。确定拒绝且没有任何成交的决策现金为0，但不计成已平仓交易；未知从不填0。主场景匹配双方无未知。', '',
        '|年份|退出规则|配对数|真实信号平均净R|对照平均净R|真实−对照|删除最佳差额后|',
        '|---|---|---:|---:|---:|---:|---:|']
    for y in ['2025','2026']:
        for model in ['BASE','F2S8']:
            q=summary[f'year/{y}/{model}/corrected_touch/slip1']
            lines.append(f"|{y}|{model}|{q['known_pairs']}|{q['mean_real']:.3f}|{q['mean_control']:.3f}|{q['mean_difference']:.3f}|{q['mean_difference_without_best']:.3f}|")
    lines += ['', '2025其中一对真实与全部对照都因原生geometry门槛拒绝，现金增量为0。它仍保留在全部决策估计中；其余两对的结果没有因此被隐藏。逐类成交与拒绝数在fill-diagnostics.json，按已平仓订单计算的均值仅作条件诊断，不能取代上表。', '',
        '这些不是组合收益，也不代表频率达标。没有把对照或单笔收益加到E20上，没有重新竞争一仓位。原E20、BASE与F2S8组合基线保持原有身份。', '',
        '## 成交压力下是否仍有增量', '',
        '|年份|退出规则|增量为正的执行场景|最差平均差额R|最好平均差额R|',
        '|---|---|---:|---:|---:|']
    for y in ['2025','2026']:
        for model in ['BASE','F2S8']:
            ds=[z['mean_difference'] for k,z in summary.items() if k.startswith(f'year/{y}/{model}/')]
            lines.append(f'|{y}|{model}|{sum(z>0 for z in ds)}/8|{min(ds):.3f}|{max(ds):.3f}|')
    lines += ['', '2026 BASE在8种场景全部落后；F2S8主场景接近零，8种仅1种为正。2025 BASE/F2S8也没有稳定的正增量。不能挑选唯一为正的压力组合来宣布收回有效。', '',
        '## 个体差额：没有隐藏失败信号', '',
        '|真实收回时刻|方向|候选数|BASE主场景差额R|F2S8主场景差额R|',
        '|---|---|---:|---:|---:|']
    for q in sorted(qualified,key=lambda z:z['ts']):
        ps={z['case']:z for z in pairs if z['key']==q['key']}
        b=ps['BASE/corrected_touch/slip1']['difference'];f=ps['F2S8/corrected_touch/slip1']['difference']
        lines.append(f"|{q['ts']}|{'多' if q['direction']=='up' else '空'}|{len(q['hosts'])}|{b:.3f}|{f:.3f}|")
    lines += ['', '## 为什么固定窗口较好，却没有形成成交优势', '',
        '2026这6个可匹配信号，在4/8/16/32柱的平均期末方向推进，相比对照分别高约0.185/0.351/0.347/0.263计划R。但它们经过止损、目标、时间退出、费用及成交守卫后，BASE仍落后，F2S8接近零。固定窗口末端价格更有利，不能替代价格先后顺序与可兑现净收益。', '',
        '2025的路径对照有效配对数从4/8柱的3个，降到16柱的2个、32柱的1个；32柱平均差额由唯一一个信号决定，不据此得出长期方向结论。逐窗口删失和控制数量保存在path-pairs.json。', '',
        '## 对照选择敏感性', '',
        '按事先固定随机种子做199次分配，每个信号选一个对照，同一次分配不复用同一普通时点；199次全部成功。主场景的配对差额分布如下。它是对照选择敏感性，既不是199份独立市场样本，也不是统计显著性或盈利概率。', '',
        '|年份|退出规则|差额中位R|差额P10|差额P90|', '|---|---|---:|---:|---:|']
    reps=m.R(m.RUN/'replications.json')
    for y in ['2025','2026']:
        for model in ['BASE','F2S8']:
            vals=sorted(q['difference'] for q in reps[f'{y}/{model}/corrected_touch/slip1'] if q['difference'] is not None)
            lines.append(f'|{y}|{model}|{st.median(vals):.3f}|{vals[int(.1*(len(vals)-1))]:.3f}|{vals[int(.9*(len(vals)-1))]:.3f}|')
    lines += ['', '这些范围均跨零，说明选到不同背景时点足以改变小样本结论。随机抽取的中位数与“每个信号平均全部对照”的主估计并非同一个统计量；不择优切换汇报口径。', '',
        '## 本轮判断与研究优先级', '',
        '1. 尚未证明双向收回A起点＋计划2R有稳定的独立信息优势；这9个严格匹配样本没有支持它。在覆盖不足的情况下，也不能断言其余33个信号或整个ABC均无效。',
        '2. 上一轮未支持“退出太早”，本轮又未得到稳定扣费增量。因此暂不继续叠加止盈、方向过滤或保持/回踩确认去拟合这段历史；固定做多继续停止。',
        '3. 下一步更有价值的是补2025年5—8月：先按固定E20、BASE、F2S8连续执行，再检查收回信号供给与相同条件的匹配覆盖。该段原B2/B5结果已看过，只能叫新增月份迁移，不是完全未见样本外。不要为了把本次9个扩成更多而事后放宽匹配条件。',
        '4. 若扩展月份仍无足够收回频率或独立增量，应降低该分支优先级，另立ABC内部的入场机制研究；当前证据不支持部署，也没有解决大几十笔成交要求。', '',
        '## 验证与产物', '',
        '- 原生执行器共输出1,216条记录，其中原42个信号×两套退出×8种场景的672条，逐项精确复现旧结果，包括手续费、成交、退出及入场拒绝；其余544条为34个控制计划。',
        '- 2025普通只读行情重取：7合约46,123分钟，完整覆盖字典及哈希与既有验证一致；拼接输入哈希也一致，1,484根原5分钟重叠OHLC/量/持仓零差异。原始分钟未另存。',
        '- 双向计划平移测试通过；匹配条件、前缀/排除区、原生基线、费后汇总和未知处理已核验。六个已有生产修改文件哈希不变。',
        '- v1尝试在首次写子目录时因目录未创建而停止，尚未写任何匹配结果；v2只修复目录创建，保持匹配与执行规则不变。旧协议保留，未删除或覆盖失败尝试。',
        '- protocol.json为先验本轮协议；各期matches.json保留全部信号与原因；execution.protocol.json及原生evaluations.jsonl保留执行依据；fee-priced-decisions.json保留计费明细。',
        '- paired-results.json、summary.json、path-pairs.json、fill-diagnostics.json、assignments.json、replications.json与analysis-verification.json可用于离线复核。',
        '- 此目录不允许覆盖重跑；新增研究必须另建编号。', '']
    with (m.RUN/'summary.md').open('x',encoding='utf-8') as f:f.write('\n'.join(lines))
    before=m.R(m.RUN/'protocol.json')['production_before']
    assert all(m.r.sha(m.r.b.ROOT/f)==h for f,h in before.items())
    m.write('completion.json',dict(status='completed',matched=9,all_signals=42,native_rows=1216,exact_baseline_rows=672,
        production_unchanged=True,report_script_sha256=m.r.sha(__file__),qualified_for_trading=False))
    print('Report completed; strict coverage 9/42, no stable positive incremental edge.')


if __name__=='__main__':run()
