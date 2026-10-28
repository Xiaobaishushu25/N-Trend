"""Report the complete same-clock routing test, including adverse evidence."""
import sys
sys.dont_write_bytecode=True
import collections
import json
import subprocess
import research_swing_jd_reclaim_routing as t

RUN=t.RUN
R=t.R
LABEL={'E20_ONLY':'E20单独','BASE':'原收回＋计划2R','F2S8':'收回＋计划2R＋F2S8退出'}
f=lambda x:f'{x:,.2f}'


def main():
    p=R(RUN/'protocol.json');rs=R(RUN/'results.json');monthly=R(RUN/'monthly-results.json');at=R(RUN/'attribution.json')
    assert p['script_sha256']==t.x.sha(t.__file__) and p['source_hashes']==t.sources()
    accepted=collections.defaultdict(list)
    for line in (RUN/'accepted-primary.jsonl').open(encoding='utf-8'):
        q=json.loads(line);accepted[q['key']].append(q['row'])
    summary={};diagnostics={}
    for year,months in [('Y2025',t.v.MONTHS),('Y2026',t.b.MONTHS)]:
        for model in LABEL:
            for route in (['E20_FIRST'] if model=='E20_ONLY' else t.ROUTES):
                key='/'.join([year,t.PRIMARY,model,route]);qs=[rs['/'.join([year,case,model,route])] for case in p['cases']]
                summary[year+'/'+model+'/'+route]=dict(primary=rs[key],min16cash=min(z['net'] for z in qs),
                    min16normalized=min(z['normalized_net'] for z in qs),cash_positive_cases=sum(z['net']>0 for z in qs),
                    max16unknown=max(z['unknown'] for z in qs))
        key=year+'/'+t.PRIMARY+'/F2S8/RECLAIM_FIRST';aa=accepted[key]
        modules={m:t.b.z.metric([q for q in aa if q['module']==m],months) for m in ['E20S1','S0_RECLAIM_R2']}
        closed=[q for q in aa if q['module']=='S0_RECLAIM_R2' and t.EV(q)['status']=='closed']
        diagnostics[year]=dict(modules=modules,reclaim_closed=[t.brief(q) for q in closed],
            reclaim_wins=sum(t.EV(q)['net_amount']>0 for q in closed),
            reclaim_targets=sum(t.EV(q)['exit_reason']=='target_s1' for q in closed),
            observed_mfe_counts={str(level):sum(t.EV(q)['mfe_r']>=level for q in closed) for level in [.5,1,1.5]},
            reclaim_ex_best_cash=modules['S0_RECLAIM_R2']['net']-max([0]+[t.EV(q)['net_amount'] for q in closed]))
    t.write('comparison-summary.json',summary);t.write('signal-diagnostics.json',diagnostics)
    lines=['# 鸡蛋收回＋计划2R：同钟信号竞争验证', '',
        '日期：2026-10-27。结论：E20固定优先确实漏掉少数更好的收回执行，但修正同钟优先级并没有解决跨年份收益不稳定，也没有增加交易频率。本轮仅作研究，未修改生产交易文件。', '',
        '## 本轮只改变一个机制', '',
        'E20与收回模块共用一个鸡蛋仓位。同一个预警时刻出现两个可用信号时，原实现按E20优先。本轮固定测试改为收回优先；不同预警时刻的顺序、已有挂单和持仓、同模块内部排序、同一A两次已实现止损上限全部保持。每一笔仍做双向、保留ABC、原计划2R和原费用/经济性筛选。', '',
        '分别核对原退出BASE、结构重失守两根15分钟确认＋停滞退出F2S8两套已存在的退出规则，没有新搜索参数。F2S8使用真实组合规则的逐笔回放，不把REFAIL2单独结果当作组合结果。', '',
        '这是看过2025年失败结果和两笔冲突之后提出的顺序假设；2025、2026均属于已观察历史，不宣称独立样本外检验。预先保存本轮协议后再计算所有结果。', '',
        '## 时间和成交口径', '',
        '- 2025：9月1日—12月31日预警，含实际退出尾部，四个月连续占仓，不按月重置。',
        '- 2026：1—6月、7月1—10日、8月10日—10月23日11:05；缺口与原研究相同。7月13—24日既有附加复核未混入主评分。',
        '- 一手净额，扣冻结手续费、逐次成交滑点。16场景＝两种入场成交方式×1—4tick/次成交×两种未知占仓政策；不是16段独立行情。统一计划风险300元结果仅供比较，不是账户收益。未知盈亏从未按0元填补。', '',
        '## 整体结果', '',
        '|时间段|方案|同钟优先|平仓数|主净额|16场景最低净额|16场景最低统一风险净额|现金盈利场景数|',
        '|---|---|---|---:|---:|---:|---:|---:|']
    for year in ['Y2025','Y2026']:
        for model in LABEL:
            for route in (['E20_FIRST'] if model=='E20_ONLY' else t.ROUTES):
                q=summary[year+'/'+model+'/'+route];z=q['primary']
                lines.append('|'+year[1:]+'|'+LABEL[model]+'|'+('E20' if route=='E20_FIRST' else '收回')+f"|{z['n']}|{f(z['net'])}|{f(q['min16cash'])}|{f(q['min16normalized'])}|{q['cash_positive_cases']}/16|")
    lines += ['', '2025改优先后的F2S8主场景虽然小赚29.39元，统一计划风险口径仍亏173.83元；16场景只有2个盈利，最低亏614.58元。2026现金收益仅增加10.02元，最差场景未改善。两个时期分别仍为15笔、41笔平仓，交易频率没有增加。2025最多未知0；2026组合最多未知1，属于原有期末未结束交易。', '',
        '## 真正改变了哪些交易', '',
        '|预警时刻|被替换E20净额|新收回交易净额|组合净改善|',
        '|---|---:|---:|---:|']
    for year in ['Y2025','Y2026']:
        z=at[year+'/'+t.PRIMARY+'/F2S8'];old={q['warning_ts']:q for q in z['removed']}
        for q in z['added']:
            prior=old[q['warning_ts']];lines.append(f"|{q['warning_ts']}|{f(prior['net'])}|{f(q['net'])}|{f(q['net']-prior['net'])}|")
    lines += ['',
        '2025年11月26日的530.29元不能全部算作错失利润：同钟E20原本也赚270.32元，所以替换净改善259.97元。10月31日两种信号都亏损，收回优先只减少约50.01元损失。2026年3月25日两种信号都抓到了同一轮上涨，替换只多赚10.02元。', '',
        '原先还存在同一模块不同A结构同钟竞争，以及已有持仓挡住后续信号；本轮没有根据它们的事后收益重新排序。所有组合重新连续排队，新增与移除交易逐笔净差均与组合差额核对一致。', '',
        '## 2025年分月：F2S8组合', '',
        '|预警月份|原E20优先净额|收回优先净额|收回优先平仓数|',
        '|---|---:|---:|---:|']
    for month in t.v.MONTHS:
        base='Y2025/'+t.PRIMARY+'/F2S8/';old=monthly[base+'E20_FIRST/'+month];new=monthly[base+'RECLAIM_FIRST/'+month]
        lines.append(f"|{month}|{f(old['net'])}|{f(new['net'])}|{new['n']}|")
    lines += ['', '12月仍没有收回决策，利润全部来自E20，不能算收回模块有效的证据。', '',
        '## 更值得关注的是有效推进和收益集中度', '',
        '|收回优先后的收回模块|2025年9—12月|2026已覆盖段|',
        '|---|---:|---:|']
    a=diagnostics['Y2025'];bb=diagnostics['Y2026']
    for label,values in [
        ('已平仓笔数',[a['modules']['S0_RECLAIM_R2']['n'],bb['modules']['S0_RECLAIM_R2']['n']]),
        ('盈利笔数',[a['reclaim_wins'],bb['reclaim_wins']]),
        ('计划目标成交笔数',[a['reclaim_targets'],bb['reclaim_targets']]),
        ('退出前MFE≥0.5实际R笔数',[a['observed_mfe_counts']['0.5'],bb['observed_mfe_counts']['0.5']]),
        ('退出前MFE≥1实际R笔数',[a['observed_mfe_counts']['1'],bb['observed_mfe_counts']['1']]),
        ('一手净额',[f(a['modules']['S0_RECLAIM_R2']['net']),f(bb['modules']['S0_RECLAIM_R2']['net'])]),
        ('去掉最大盈利交易后净额',[f(a['reclaim_ex_best_cash']),f(bb['reclaim_ex_best_cash'])])]:
        lines.append('|'+label+'|'+'|'.join(str(v) for v in values)+'|')
    lines += ['',
        '2025年7笔新增收回交易仅2笔盈利、1笔达到计划目标。模块合计赚183.83元，去掉最大盈利530.29元后反而亏346.46元；整个组合去掉该笔后亏500.89元。2026收回模块有23笔平仓、10笔盈利、7笔目标成交，仍有1笔未知；不能只凭正收益月份数量就认为盈利分布稳定。', '',
        '2025部分亏损信号也曾走出0.79—0.84实际R，说明并非完全没有顺向推进；另有几笔MFE只有0.05、0.08、0.29R。把全部问题归于止盈止损并不符合逐笔证据。这里MFE/MAE只观察到策略真实退出，分母是含入场滑点后的实际风险，数值会被提前退出截断，不能代替“即使退出也继续观察固定K线窗口”的信号质量检验，也不能直接混作计划R。', '',
        '|2025收回预警|方向|一手净额|退出前MFE实际R|退出前MAE实际R|退出原因|',
        '|---|---|---:|---:|---:|---|']
    for q in a['reclaim_closed']:
        lines.append(f"|{q['warning_ts']}|{'多' if q['direction']=='up' else '空'}|{f(q['net'])}|{q['mfe_r']:.2f}|{q['mae_r']:.2f}|{q['exit_reason']}|")
    lines += ['', '## 本轮判断与下一步', '',
        '1. 同钟优先是一个确实存在的执行交互，但它只替换了3个时刻的交易，无法解释整体跨时期差异。保存收回优先作为候选，不升级正式策略。',
        '2. 暂停继续搜索止盈或按月份/方向补丁。下一轮先补齐全部合格收回预警在固定4、8、16、32根15分钟K线的推进，策略退出后仍继续观察；用计划风险作共同分母，同时记录先到+1R、+2R还是−1R，区分“信号只预测短回弹”与“信号有延续但退出漏掉”。遇同根触碰两端保持路径不确定。',
        '3. 这类固定窗口诊断需要重新读取分钟行情。当前保存的是逐笔成交和退出前极值，不能据此虚构退出后的行情路径。需要将2025年更早月份留作下一个固定规则的时间检验；当前两段都已看过，不再包装成新的样本外。',
        '4. 用户希望15分钟级别达到大几十笔；优先级和退出优化不会创造新预警。若固定窗口证据支持收回入场，再单独研究ABC内的其他可交易触发；否则应先调整收回预警的确认机制。EG/PVC完整B5继续保持。', '',
        '## 复核与边界', '',
        '已逐项重现96个已发布基线场景；1632行几何/经济性、272行BASE、1088行退出修改入场一致性校验通过。离线标准库包重新排队，核对1280项整体和分月统计。六个既有生产修改文件哈希保持不变；原冻结候选全部391个文件通过哈希检查。', '',
        '本轮只重算订单分配，沿用冻结的分钟执行结果，没有重新模拟不同成交规则。报告中的回撤若引用均为已实现平仓权益回撤，不是分钟盯市回撤；此次不声称改善盘中风险。', '',
        '证据：[测试前协议](protocol.json) · [全部场景](results.json) · [逐月结果](monthly-results.json) · [逐笔替换归因](attribution.json) · [被挡信号及当时挂单状态](primary-conflicts.json) · [信号推进诊断](signal-diagnostics.json) · [离线回放](offline-order-replay/replay.py) · [核验](verification.json)。', '']
    with (RUN/'summary.md').open('x',encoding='utf-8') as out:out.write('\n'.join(lines))
    replay=subprocess.run(['python',str(RUN/'offline-order-replay/replay.py')],capture_output=True,text=True,check=True)
    verification=R(RUN/'verification.json');assert verification['production_unchanged']
    t.write('completion.json',dict(reporter_sha256=t.x.sha(__file__),offline_replay=json.loads(replay.stdout),
        production_unchanged=all(t.x.sha(t.b.ROOT/name)==h for name,h in p['production_before'].items()),
        inputs_unchanged=p['source_hashes']==t.sources(),
        outputs={str(q.relative_to(RUN)):t.x.sha(q) for q in RUN.rglob('*') if q.is_file()}))
    print(json.dumps(dict(report=str(RUN/'summary.md'),offline=json.loads(replay.stdout),production_unchanged=True),ensure_ascii=False))


if __name__=='__main__':main()
