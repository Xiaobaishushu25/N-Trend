"""Report and independent invariants for the frozen forward-path audit."""
import sys
sys.dont_write_bytecode=True
import collections
import json
import statistics as st
import audit_swing_jd_reclaim_forward as a


def run():
    rows=sum([a.R(a.RUN/(s+'-paths.json')) for s in ['early','recent','validation2025']],[])
    assert len(rows)==168 and len({(q['key'],q['horizon']) for q in rows})==168
    for q in rows:
        if q['status']=='ok':
            for kind in ['signal','execution']:
                if q.get(kind):
                    v=q[kind]
                    assert -v['mae']-1e-9<=v['end_r']<=v['mfe']+1e-9
        else:
            assert 'signal' not in q and 'execution' not in q
    fees=a.R(a.r.b.ROOT/'scripts/exchange_fees_20261022.json')['products']['JD']
    assert all(fees[k]==dict(fixed_yuan_per_lot=0.,notional_rate=.00015) for k in ['open','close_today','close_yesterday'])
    accepted=collections.defaultdict(set)
    for line in (a.routing.RUN/'accepted-primary.jsonl').open(encoding='utf-8'):
        z=json.loads(line);r=z['row']
        if '/F2S8/' in z['key'] and r['module']=='S0_RECLAIM_R2':
            year=z['key'].split('/')[0][1:];route=z['key'].split('/')[-1]
            accepted[year,route].add(r['original_event_key'])
    extra={}
    for y in ['2025','2026']:
        for n in a.HORIZONS:
            cohort=[q for q in rows if q['year']==y and q['horizon']==n]
            for name,keys in [('all',None)]+[(route,accepted[y,route]) for route in a.routing.ROUTES]:
                xs=[q for q in cohort if keys is None or q['key'] in keys]
                extra[y+'/'+str(n)+'/'+name]=a.summarize(xs)
    a.write('accepted-subset-summary.json',extra)
    a.write('verification.json',dict(unique_decisions=42,rows_checked=168,
        excursion_bounds=True,censored_not_zero=True,fee_formula_matches_frozen_JD=True,
        report_script_sha256=a.r.sha(__file__),
        accepted_source_sha256=a.r.sha(a.routing.RUN/'accepted-primary.jsonl'),
        underlying_script_hash_unchanged=a.R(a.RUN/'protocol.json')['script_sha256']==a.r.sha(a.__file__)))
    lines=['# 鸡蛋收回信号：退出后固定窗口诊断', '',
        '日期：2026-10-27（北京时间）。本轮继续交接文档中的第一、二步，完成全部42个收回决策的4/8/16/32根15分钟路径审计。没有新策略通过验收。', '',
        '主要结论：现有证据不支持用“退出太早”解释2025迁移失败，也不支持直接把双向收回2R改成更长持仓或更宽止损。2025短窗口虽常出现有利波动，但期末平均推进仍负；2026较长窗口较好，优势仍有明显跨期差异。', '',
        '## 样本与口径', '',
        '- 2026：32个全部收回决策，覆盖1—6月、7月1—10日、8月10日—10月23日已有开发段；不混入7月中旬附加复核。2025：9—12月全部10个收回决策。均非新样本外。',
        '- 原始2025行情通过既有普通TqBacktest/TqSim只读重取，7个实际合约、46,123分钟，与上一轮完整覆盖字典及序列哈希一致；原始分钟仅驻留内存。',
        '- 信号线从收回决策收盘之后开始，分母统一为决策时计划风险；MAE取正幅度。执行线从原生F2S8实际可成交开盘开始，包含不利入场滑点；仍用计划风险，便于同信号比较。',
        '- 观察不因原策略退出而结束。每根15分钟必须有完整15条正成交量分钟，休市不算新柱；换主力、缺分钟、零量、归档尾部不足均明确删失。每个窗口分别要求完整，因此不同窗口样本数有差异。',
        '- 首次触达以分钟开盘先判跳空；同分钟高低同时触线而开盘无法判序，记same_minute_unknown，不选择有利路径。本次两类比赛均未出现此歧义。',
        '- 不按赢家或接受订单筛选。全部42条原生economic_pass字段均为true，但它不代表所有资格检查都通过：2025有2条filtered_geometry；2026有filtered_geometry、filtered_actual_risk、filtered_A_net_body各1条。故另外单列有原生入场的8/29条。29条含1笔尚未结束，未把其策略收益填0。',
        '- 父A簇、月份、季度、方向分组见summary.json；两种同钟排序下的真实组合接受子集见accepted-subset-summary.json。它们只是子集诊断，没有重新优化或相加组合利润。', '',
        '## 全部信号：固定窗口路径', '',
        '|年份|后续15分钟柱|完整/全部|MFE中位R|MAE中位R|期末平均R|先＋2R|先−1R|两者未到|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for y in ['2025','2026']:
        for n in a.HORIZONS:
            xs=[q for q in rows if q['year']==y and q['horizon']==n]
            q=a.summarize(xs);v=q['signal'];race=v['race2']
            lines.append(f"|{y}|{n}|{q['complete']}/{q['total']}|{v['mfe']['median']:.3f}|{v['mae']['median']:.3f}|{v['end_r']['mean']:.3f}|{race.get('upper_first',0)}|{race.get('stop_first',0)}|{race.get('neither',0)}|")
    lines += ['', '这些是路径统计，不是交易胜率。未触达者不能当亏损或盈利；计划2R毛保本率也不能直接用于上述删失样本来推断策略期望。', '',
              '## 真实退出是否截断了后续目标', '',
              '|年份|32柱内完整且有入场|非目标退出后又到原目标|其中原止损尚未先到|',
              '|---|---:|---:|---:|']
    late=[]
    for y in ['2025','2026']:
        xs=[q for q in rows if q['year']==y and q['horizon']==32 and q['status']=='ok' and q.get('execution')]
        missed=[q for q in xs if q['native_exit']!='target_s1' and q.get('after_exit',{}).get('target_hit')]
        safe=[q for q in missed if q['execution']['planned_target_race']=='upper_first']
        lines.append(f'|{y}|{len(xs)}|{len(missed)}|{len(safe)}|');late+=missed
    assert len(late)==1 and late[0]['warning_ts']=='2026-06-08 11:15:00' and late[0]['native_exit']=='stop'
    lines += ['', '唯一案例是2026-06-08 11:15收回信号：先实际止损，32柱内后来碰到目标，且退出后重新观察也先碰原止损边界。它不是可通过取消F2/S8退出无风险恢复的利润。其他退出后目标重触都来自原本已止盈的交易，不计“错失目标”。',
              '', '在本次完整32柱窗口中，未发现F2或S8退出后又到原目标的案例。此结论只针对可观察窗口；换月、尾部删失及32柱以后的走势没有据此推断。', '',
              '## 提前结束持仓是否天然更好', '',
              '以下仅是有原生入场的个体信号，在窗口末收盘估值并扣1tick退出滑点与冻结手续费；允许价格越过原止损继续观察，忽略最长持仓、节假日前退出和仓位竞争。它不是可执行新策略收益，不能与E20相加。', '',
              '|年份|4柱平均净R|8柱平均净R|16柱平均净R|32柱平均净R|',
              '|---|---:|---:|---:|---:|']
    for y in ['2025','2026']:
        vals=[]
        for n in a.HORIZONS:
            xs=[q['execution']['fixed_window_net_r'] for q in rows if q['year']==y and q['horizon']==n and q.get('execution')]
            vals.append(f'{st.mean(xs):.3f}')
        lines.append('|'+y+'|'+'|'.join(vals)+'|')
    lines += ['', '这不支持“收回都是短回弹，所以统一早点结束就能赚钱”。2025四个窗口平均估值均为负；2026前两个窗口也为负。仍不能据此否定某个尚未验证的路径依赖止盈，但没有理由继续盲扫退出参数。', '',
              '## 同一批完整32柱信号的跨窗口复核', '',
              '为避免16/32柱删除部分样本改变表面走势，下表固定为32柱完整的同一批信号，再比较四个窗口的平均期末方向R。', '',
              '|年份|固定信号数|4柱|8柱|16柱|32柱|', '|---|---:|---:|---:|---:|---:|']
    for y in ['2025','2026']:
        keys={q['key'] for q in rows if q['year']==y and q['horizon']==32 and q['status']=='ok'}
        values=[st.mean(q['signal']['end_r'] for q in rows if q['key'] in keys and q['horizon']==n) for n in a.HORIZONS]
        lines.append('|'+y+'|'+str(len(keys))+'|'+'|'.join(f'{v:.3f}' for v in values)+'|')
    lines += ['', '## 分月16柱诊断', '', '|预警月份|完整/全部|平均期末R|先＋2R|先−1R|', '|---|---:|---:|---:|---:|']
    for m in sorted({q['month'] for q in rows}):
        q=a.summarize([z for z in rows if z['month']==m and z['horizon']==16]);v=q['signal']
        lines.append(f"|{m}|{q['complete']}/{q['total']}|{v['end_r']['mean']:.3f}|{v['race2'].get('upper_first',0)}|{v['race2'].get('stop_first',0)}|")
    lines += ['', '2025年12月没有收回决策，因此不出现在分月表中；该月原组合盈利不能当作收回有效的证据。', '',
              '## 研究结论与下一步', '',
              '1. 保留双向ABC收回＋计划2R作为观察候选，E20、BASE、F2S8完整对照不变。现有结果仍未满足频率、跨期稳定性与成交压力的联合要求。',
              '2. 暂不继续扩大止损、延长持有或扫描止盈点；新证据未发现足够的被提前退出截断的原2R机会。也不据方向分组重新启动固定做多。',
              '3. 下一优先级转为入场信号的信息增量：同合约、同月份、同15分钟时刻、前序动量与波动背景的严格匹配，并把相同计划风险几何、原生费用和成交守卫用于对照。旧15信号ATR匹配仍不足以代表本次42个决策的可交易优势。此项本轮未完成，不能声称收回优于匹配背景。',
              '4. 只有匹配后仍有增量，才值得单独冻结一种收回后保持或回踩确认假设；更晚入场可能耗掉空间并进一步降低频率，不能凭当前路径表推荐其部署。2025年5—8月可用于固定规则迁移，需重新审计行情且承认B2/B5历史已看过。', '',
              '## 复核与证据', '',
              '- 2项针对分钟先后与缺失/换月的测试通过；168条窗口记录的唯一性、极值边界、删失非零填补和冻结费率独立核验通过。',
              '- 2025重取数据与既有迁移研究完全一致；加载沿用原生计费订单。来源哈希保存在sources.json和verification.json，协议在看本次结果前落盘。',
              '- 六个已有生产修改文件的前后哈希一致。原冻结快照、既有研究目录和策略规则均未覆盖。',
              '- 运行脚本：scripts/audit_swing_jd_reclaim_forward.py；报告与核验：scripts/report_swing_jd_reclaim_forward.py。审计入口采用新目录独占创建，不能直接覆盖重跑。',
              '- 原始逐笔：early-paths.json、recent-paths.json、validation2025-paths.json。completion.json记录42决策与168窗口；summary.json保留全部月份、季度、方向和父A分组。', '']
    with (a.RUN/'summary.md').open('x',encoding='utf-8') as f:f.write('\n'.join(lines))
    print('Verified 168 windows, report written.',flush=True)


if __name__=='__main__':run()
