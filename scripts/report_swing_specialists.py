"""Reconcile baseline, explain product/exit economics, report bounded validation."""
import collections
import datetime as dt
import json
import math
import random
import statistics
import research_swing_specialists as s

RUN=s.ARCHIVE
read=s.m.old.read
def write(name,obj):
    path=RUN/name
    if path.exists():
        assert read(path)==obj, 'Existing numeric evidence changed: '+name
    else:s.m.old.write(path,obj)
ev=s.m.report.s.ev


def baseline():
    c,pp,events,ends,data,sources,coverage=s.m.load()
    p=read(s.MAY/'protocol.json');cal=s.TradingDates({});cal.dates=read(s.MAY/'trading-dates.json')
    older=s.m.report.row_cases(s.MAY/'older/evaluations.jsonl','unmerged','may_aug',read(p['fees_path']),cal)
    events=events['unmerged'];ends=ends['unmerged']
    pool=read(s.MAY/'older-pool.json')
    assert not events.keys() & {z['event_key'] for z in pool}
    events.update({z['event_key']:z['event'] for z in pool})
    for code,end in read(s.MAY/'older/manifest.json')['contract_ends'].items():ends[code]=max(end,ends.get(code,''))
    rows=[z for z in data['unmerged']['budget_body',s.m.PRIMARY]+older['budget_body',s.m.PRIMARY] if z['symbol'] in s.SIX]
    selected=[z for z in rows if z['symbol'] in s.FOUR]
    aa,ll,risk=c.select(selected,events,ends,pp['sector_map'],'sector2_counter_stops','original')
    s.m.report.audit(ll,selected)
    z=s.metric(aa,events)
    reference=read(s.MAY/'combined18-results.json')['original']['B5'][s.m.PRIMARY]['four_EG_V_JD_SR']
    for k in ['n','net','wins','losses','mean_r','realized_cash_drawdown','unknown','bins','warning_month_net']:
        assert s.m.equal(z[k],reference[k]),k
    z['products']={symbol:s.metric([r for r in aa if r['symbol']==symbol],events) for symbol in s.FOUR}
    paths=[s.MAY/'older-pool.json',s.m.old.RUN/'unmerged/older-pool.json',s.NATIVE/'early-pool.json',s.NATIVE/'recent-pool.json']
    snaps={x['event_key']:x for path in paths for x in read(path)}
    diagnostic={}
    for sym in s.SIX:
        individual=[r for r in rows if r['symbol']==sym]
        trades=[r for r in aa if r['symbol']==sym and ev(r)['status']=='closed']
        def group(fun):
            bins=collections.defaultdict(list)
            for r in trades:bins[fun(r)].append(r)
            return {str(k):dict(n=len(v),net=sum(ev(r)['net_amount'] for r in v),mean_r=statistics.mean(ev(r)['net_r'] for r in v)) for k,v in bins.items()}
        diagnostic[sym]=dict(warnings=len(individual),individual_statuses=dict(collections.Counter(ev(r)['status'] for r in individual)),
            median_planned_stop_ticks=statistics.median(r['planned_risk']/r['tick_size'] for r in individual),
            median_warning_ATR_ticks=statistics.median(snaps[r['event_key']]['atr14']/r['tick_size'] for r in individual),
            closed=len(trades),mean_cash_risk=statistics.mean(ev(r)['risk']*r['multiplier'] for r in trades) if trades else None,
            median_plan_cash_cost=statistics.median(r['new_friction']*r['planned_risk']*r['multiplier'] for r in trades) if trades else None,
            exits=group(lambda r:ev(r)['exit_reason']),direction=group(lambda r:r['direction']),
            hour=group(lambda r:('missing' if (events[r['event_key']].get('market_context') or {}).get('trend_alignment_60') is None else
                        'aligned' if events[r['event_key']]['market_context']['trend_alignment_60']>0 else 'counter_or_neutral')),
            mfe_ge1_loss_n=sum(ev(r)['mfe_r']>=1 and ev(r)['net_amount']<0 for r in trades),
            no_follow_n=sum(ev(r)['exit_reason']=='no_follow' for r in trades))
    write('baseline18-diagnostics.json',dict(metrics=z,products=diagnostic,exact_continuous_control=True,
          accepted_n=len(aa),risk={k:v for k,v in risk.items() if k!='realized_settlements'},
          note='Warnings include nonfilled and filtered plans; only executed four-product trades enter directional/exit statistics. PP/CJ final trade metrics are in original six-product comparator.'))
    return z,diagnostic


def fmt(v):return '—' if v is None else f'{v:,.2f}'
def pct(v):return '—' if v is None else f'{v:.1%}'


def uncertainty(deltas):
    """Paired three-month circular block resampling; descriptive, not a p-value."""
    rng=random.Random(20261026);n=len(deltas);xs=[]
    for _ in range(4000):
        sample=[]
        while len(sample)<n:
            j=rng.randrange(n)
            sample.extend(deltas[(j+i)%n] for i in range(3))
        xs.append(sum(sample[:n]))
    xs.sort()
    return dict(n_months=n,block_months=3,replications=4000,delta=sum(deltas),
                descriptive_95pct_interval=[xs[100],xs[3899]],
                caution='Very few dependent months, overlapping blocks, incomplete coverage, already selected rules/products. Not a selection-adjusted confidence interval or future-profit probability.')


def report():
    b,d=baseline()
    scopes={}
    intervals={}
    for scope in ['cost14','native2026']:
        p=RUN/scope
        scopes[scope]=dict(protocol=read(p/'protocol.json'),fixed=read(p/'fixed-results.json'),wf=read(p/'walkforward-results.json'),
                           folds=read(p/'fold-results.json'),selection=read(p/'selection-before-test.json'),attr=read(p/'attribution.json'))
        f=scopes[scope]['folds']['four/original/'+s.m.PRIMARY]
        deltas=[a['selected']['warning_month_net'][mo]-a['B5']['warning_month_net'][mo] for a in f for mo in a['selected']['warning_month_net']]
        intervals[scope]=uncertainty(deltas)
    write('uncertainty.json',intervals)
    lines=['# 少品种专精：盈亏归因与按时间分组验证','',
        '研究日期：2026-10-26（北京时间）。主组合固定 EG＋PVC＋JD＋SR，六品种 EG/PP/PVC/JD/SR/CJ 仅作名单外推对照。保留原B2、B5；本轮没有修改生产策略或接入实盘。',
        '', '已有18个月基线可复核；本轮旧分钟行情服务多次初始化/握手失败。因此成本门槛研究覆盖2025-09—2026-10，退出研究仅覆盖2026-01—10，不能将退出改动称为已完成18个月验证。7月、10月及JD近期覆盖不完整，沿用原数据边界。',
        '', '## 1. 主要判断','',
        '1. 四品种的市场驱动确实有差异，但现有ABC策略的盈利来源仍以EG为主。专精名单能减少研究维度，不能自动消除共同风险或建立农产品优势。',
        '2. 鸡蛋低频有明确的经济性门槛原因。提高频率最明显的候选是农产品保留原S1目标，但这同时改变目标、计划资格和持仓占用，需要完整重新竞争机会。',
        '3. 延后无跟随退出并未稳定提高主场景收益；全样本表现更好也未充分转化为滚动测试改善。下面分别列开发统计、训练期选择和后续组结果，不凭最高历史净额升级策略。',
        '', '## 2. 品种特点与适配含义','',
        '|品种|主要驱动与结构|对当前策略的研究含义|', '|---|---|---|',
        '|EG 乙二醇|油制/煤制成本、聚酯需求和区域物流库存共同影响；本批化工利润主力|保留趋势收益来源；重点审计亏损月份和同时持仓现金风险，不用事后月份白名单追逐利润|',
        '|PVC 聚氯乙烯|电石/乙烯两条工艺，上游能源与下游加工需求共同影响|与EG有共同化工敞口，但不是同一个产业需求；本策略小优势易受执行摩擦侵蚀|',
        '|JD 鸡蛋|生鲜、储运约束，存栏、饲料成本、天气、疫病及节日需求影响；合约月份预期需单独看|报价单位不能误当每点5元，本研究每点每手10元；不把现货季节性直接写成盘面多空规则|',
        '|SR 白糖|国内外榨季、原糖供给、进口与库存、天气影响，且内外盘传导未必同步|利润来源可独立于化工，但本ABC规则尚未证明足够厚的交易优势；放宽准入容易增加摩擦|',
        '|PP 聚丙烯（对照）|丙烯产业链，多种原料路线和下游需求；与EG存在化工共同因子|可保留收益对照，不能把另一化工品当成独立风险来源|',
        '|CJ 红枣（对照）|新疆主产区收成、天气、产销库存与等级差异影响|稀疏交易和大波动容易让少数案例主导结论，本轮不为提高非化工占比强行加入主组合|',
        '', '产业信息依据：[EG产业分析](https://www.chemnews.com.cn/c/2025-08-18/762862.shtml)、[PVC工艺产业说明](https://www.cciac.org.cn/detail.htm?id=30006)、[中期协鸡蛋期货教材目录与合约单位](https://edu.cfachina.org/zsyd/tjsj/qhpzcsxlcs/201510/t20151023_21384.html)、[白糖榨季与进口分析](https://www.ccbfutures.com/main/a/20241104/69917.shtml?catalogno=sugar&headercatalogno=information_center)、[丙烯产业链研究](https://www.xzfutures.com/files/document/20250721/6388871245795117181717934.pdf)、[红枣产区分析](https://www.ccbfutures.com/main/a/20250814/74523.shtml?headercatalogno=information_center&type=1)。这些只解释产业驱动，未把报告中的后来信息作为历史信号特征。策略适配判断是本批实证和研究推论。',
        '', '## 3. 原B5：18个月连续组合与盈亏分布','',
        f"主场景已知平仓 {b['n']} 笔，净额 {fmt(b['net'])} 元，胜率 {pct(b['win_rate'])}，平均净R {b['mean_r']:.3f}，已实现最大回撤 {fmt(b['realized_cash_drawdown'])} 元。未知 {b['unknown']} 笔。",
        '', '|品种|平仓|净额/元|净额占比|胜率|平均净R|现金利润因子|手续费/元|去掉最佳月份净额/元|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for sym,z in b['products'].items():
        lines.append(f"|{s.m.NAMES[sym]}|{z['n']}|{fmt(z['net'])}|{pct(z['net']/b['net'])}|{pct(z['win_rate'])}|{z['mean_r']:.3f}|{fmt(z['profit_factor'])}|{fmt(z['commission'])}|{fmt(z['net_without_best_month'])}|")
    lines+=['',f"组合去掉最盈利的一笔后净额 {fmt(b['net_without_best_trade'])} 元，去掉前5笔后 {fmt(b['net_without_top5_trades'])} 元；最大盈利月份占正收益月份合计的 {pct(b['largest_positive_month_share'])}。前5笔占所有盈利交易盈利总和 {pct(b['top5_share_of_gross_wins'])}。这些是集中度压力诊断，不把删掉赢家当作可交易策略。",
        '', '正贡献品种HHI＝各品种正净贡献占比的平方和；四品种等贡献为0.25，单一来源为1。亏损品种不会被负占比“抵消”集中度。月度按预警月，现金回撤按退出顺序；二者不可互换。',
        '', '### 3.1 信号漏斗与止损现金风险','', '|品种|可回放候选|个体经济性过滤|个体几何过滤|最终四品种平仓|平均实际初始现金风险/元|计划往返成本中位/元|', '|---|---:|---:|---:|---:|---:|---:|']
    for sym in s.FOUR:
        q=d[sym];status=q['individual_statuses']
        lines.append(f"|{s.m.NAMES[sym]}|{q['warnings']}|{status.get('filtered_entry_economics',0)}|{status.get('filtered_geometry',0)}|{q['closed']}|{fmt(q['mean_cash_risk'])}|{fmt(q['median_plan_cash_cost'])}|")
    lines+=['','可回放候选已经过服务去重、形态资格与评分≥2.5筛选，并非全部原始预警。过滤列是引擎按优先顺序给出的首个失败状态，不能相加解释为独立因果损失；最终平仓还受触发、实际成交守卫、占用等影响。每次一手不是等风险，不能将现金收益差全部解释为预测能力差。',
            '', '以计划往返成本30元、目标毛空间250元为例，成本比12%会被10%门槛挡下，20%可通过这一项；止损宽度、净盈亏比、触发和实际成交守卫仍要独立满足。提高该门槛不会自动让新交易盈利。',
            '', '### 3.2 平仓原因归因','', '|品种|退出原因|笔数|净额/元|平均净R|', '|---|---|---:|---:|---:|']
    for sym in s.FOUR:
        for reason,q in d[sym]['exits'].items():lines.append(f"|{s.m.NAMES[sym]}|{reason}|{q['n']}|{fmt(q['net'])}|{q['mean_r']:.3f}|")
    lines+=['', '`target_s1`是引擎的目标退出标签；基线使用局部截近目标时也沿用此标签，不能仅凭名字认定该笔到达原始S1。第5/8桶按冻结持仓桶口径计数，不是连续自然时间。MFE是持仓期间路径的事后诊断，只用于解释，未用来决定历史入场。亏损和止损不都代表“止损太紧”；延后退出需要新的分钟路径回放，不能把离场后上涨直接算成可赚利润。',
            '', '### 3.3 多空与小时状态依赖','', '|品种|多头笔数/净额|空头笔数/净额|小时顺向笔数/净额|中性或反向笔数/净额|', '|---|---:|---:|---:|---:|']
    for sym in s.FOUR:
        q=d[sym]
        def label(v):return f"{v.get('n',0)} / {fmt(v.get('net',0))}"
        lines.append(f"|{s.m.NAMES[sym]}|{label(q['direction'].get('up',{}))}|{label(q['direction'].get('down',{}))}|{label(q['hour'].get('aligned',{}))}|{label(q['hour'].get('counter_or_neutral',{}))}|")
    lines+=['', 'EG多头38笔贡献14,262元，约占其净额95.2%；空头50笔仅720元。小时信息缺失的3笔EG另贡献2,132元，没有混入中性/反向组。鸡蛋19笔中17笔小时顺向；白糖中性/反向组净亏926元。它们提示方向和行情状态依赖，不能据此立即删除空头或非顺向机会；那将是新的规则，需要重新竞争并验证。',
            '', '## 4. 如何控制选择偏差','',
            '- 全部候选阈值事先固定：成本比10%→20%、局部目标→原S1、无跟随第5桶→第8桶。共7个原生执行模板、11个组合版本（含B2/B5两个对照），没有看到结果后继续扫描阈值、月份或品种名单。',
            '- 成本研究：2025年9—12月初始训练；后续2026年1—3、4—6、7—9、10月四组依次测试。退出研究：2026年1—3月初始训练；随后三组测试。每组只用此前严格已退出交易选规则，未结束交易不进入训练盈亏。',
            '- 训练要求至少20笔组合平仓、至少5笔农产品平仓；8场景净额均不低于B5，主场景农产品净额提高、品种HHI不恶化、删最佳交易后不弱于B5、截点未结束占用不增加。合格者按“8场景最小去最佳交易净额−0.25×8场景最大已实现回撤”取最高，否则B5。',
            '- 名单与参数在一个测试组内固定；规则在预警时选择。跨月/组不重置订单、持仓或同A止损记录。每个版本从全潜在机会重新竞争，未从旧接受表删除亏损交易。',
            '- 原始18个月已经用来研究过B2/B5、名单及部分假设。本轮只能检验时间迁移稳定性，不能保证不过拟合，不能宣称完全独立样本外验证。两种未知政策与四执行压力不是8份独立历史样本。',
            '', '## 5. 固定候选的全覆盖统计（开发描述，不用于事后挑唯一赢家）','']
    for scope,title in [('cost14','成本门槛：14个月'),('native2026','退出及联合改动：2026已有覆盖')]:
        a=scopes[scope];fixed=a['fixed'];lines+=['',f'### {title}','', '|版本|平仓|净额/元|农产品净额/元|已实现回撤/元|8场景最低净额/元|正贡献HHI|', '|---|---:|---:|---:|---:|---:|---:|']
        for name in a['protocol']['variants']:
            z=fixed['four/original/'+s.m.PRIMARY+'/'+name]
            worst=min(v['net'] for k,v in fixed.items() if k.startswith('four/') and k.endswith('/'+name))
            lines.append(f"|{s.LABELS[name]}|{z['n']}|{fmt(z['net'])}|{fmt(z['agriculture_net'])}|{fmt(z['realized_cash_drawdown'])}|{fmt(worst)}|{z['positive_product_hhi']:.3f}|")
    lines+=['', '### 5.1 鸡蛋与白糖：提高频率是否提高净收益（2026同覆盖）','', '|品种/版本|平仓|净额/元|平均净R|8场景本品种最低净额/元|', '|---|---:|---:|---:|---:|']
    fixed=scopes['native2026']['fixed']
    for sym in ['JD0','SR0']:
        for name in ['B5','JD20','AG_S1','AG_N8','AG20_S1']:
            z=fixed['four/original/'+s.m.PRIMARY+'/'+name]['products'][sym]
            worst=min(v['products'][sym]['net'] for k,v in fixed.items() if k.startswith('four/') and k.endswith('/'+name))
            lines.append(f"|{s.m.NAMES[sym]} / {s.LABELS[name]}|{z['n']}|{fmt(z['net'])}|{z['mean_r']:.3f}|{fmt(worst)}|")
    lines+=['','原S1目标更远既可能保留趋势利润，也可能增加回吐、占用和新增不盈利交易；不能只把原先目标退出的交易延长。',
            '', '## 6. 真正按组向前选择后的结果','']
    for scope,title in [('cost14','14个月成本研究：2026测试'),('native2026','2026退出研究：4—10月测试')]:
        a=scopes[scope];folds=a['folds']['four/original/'+s.m.PRIMARY];w=a['wf']['four/original/'+s.m.PRIMARY]
        bn=sum(f['B5']['net'] for f in folds)
        lines+=['',f'### {title}','', '|测试组|此前训练选定版本|B5净额/元|选定策略净额/元|差额/元|', '|---|---|---:|---:|---:|']
        for f in folds:lines.append(f"|{f['start'][:7]} 至 {f['end'][:7]}（右端不含）|{s.LABELS[f['variant']]}|{fmt(f['B5']['net'])}|{fmt(f['selected']['net'])}|{fmt(f['selected']['net']-f['B5']['net'])}|")
        lines += ['',f"测试组合计：B5 {fmt(bn)} 元，滚动策略 {fmt(w['net'])} 元，增量 {fmt(w['net']-bn)} 元；滚动已实现回撤 {fmt(w['realized_cash_drawdown'])} 元，平仓 {w['n']} 笔，农产品净额 {fmt(w['agriculture_net'])} 元，品种HHI {w['positive_product_hhi']:.3f}。",
                  '', '|政策/场景|滚动净额/元|相对同场景B5差额/元|农产品净额/元|未知|', '|---|---:|---:|---:|---:|']
        for pol in ['original','two_paths_flat']:
            for sf in s.m.SUFFIXES:
                k='four/'+pol+'/'+sf;z=a['wf'][k];control=sum(f['B5']['net'] for f in a['folds'][k])
                lines.append(f"|{pol}/{sf}|{fmt(z['net'])}|{fmt(z['net']-control)}|{fmt(z['agriculture_net'])}|{z['unknown']}|")
        ci=intervals[scope]['descriptive_95pct_interval']
        lines += ['',f"配对月度增量的3个月块重采样描述区间（4000次）约 [{fmt(ci[0])}, {fmt(ci[1])}] 元。只有{intervals[scope]['n_months']}个月且已参与研究，区间没有校正策略挑选，不是未来收益置信保证；若区间跨0，更不能把小幅增量认定为稳定优势。"]
    lines+=['', '## 7. 收益从何而来与六品种外推','', '|2026候选|净额变化/元|共同交易净额变化/元|新纳入平仓数/净额|未再纳入平仓数/原净额|', '|---|---:|---:|---:|---:|']
    for name in ['JD20','AG_S1','AG_N8','AG20_S1']:
        a=scopes['native2026']['attr'][name]
        lines.append(f"|{s.LABELS[name]}|{fmt(a['net_delta'])}|{fmt(a['common_net_delta'])}|{a['added_n']} / {fmt(a['added_net'])}|{a['removed_n']} / {fmt(a['removed_net'])}|")
    lines+=['', '|研究范围|四品种滚动测试净额/元|六品种同一日程净额/元|六品种8场景最低净额/元|', '|---|---:|---:|---:|']
    for scope,a in scopes.items():
        four=a['wf']['four/original/'+s.m.PRIMARY];six=a['wf']['six/original/'+s.m.PRIMARY]
        worst=min(z['net'] for k,z in a['wf'].items() if k.startswith('six/'))
        lines.append(f"|{scope}|{fmt(four['net'])}|{fmt(six['net'])}|{fmt(worst)}|")
    lines+=['','六品种照搬四品种训练选择日程，没有在六品种上重选规则；这个对照仍不是新市场/新历史的独立验证。',
        '', '## 8. 后续策略研究优先级','',
        '1. B2/B5继续保留。仅JD20%是可继续观察的低复杂度分支，但目前新增样本稀少；把“只多几笔盈利”写成证据薄弱，不把更高净额直接当成通用规律。',
        '2. 原S1目标值得继续研究，但应拆开鸡蛋与白糖。2026开发统计中，“20%＋S1”的JD为20笔、2,107.04元，8场景最低225.68元；同配置SR为47笔、−54元，最差−1,310元，不能因为同属农产品就统一放宽。本轮未追加事后挑选的“只JD用此组合”回测；先补齐2025年的分钟执行压力，再用未参与研究的新行情检验。',
        '3. 不将第8桶无跟随或农产品统一20%视为默认升级。高频必须以扣费后的跨组净期望为依据；白糖没有明确改善时可以少交易。',
        '4. 分散化下一步应开发非化工的独立信号来源，并按现金止损风险检查化工合计敞口；当前信号池中的过滤与退出变体无法凭空创造独立收益来源。鸡蛋季节性/合约期限、白糖内外盘等方向需要时点一致的额外数据和多年样本，本批未回测，不能当成已验证策略。',
        '5. 本轮未启动纸面采集或自动交易。未来独立行情、逐日费用、持仓浮动权益与保证金风险仍需补充；当前金额是一手已知平仓净额，不是账户年化或资金收益率。',
        '', '## 9. 证据与复现','',
        '- 初始冻结：[protocol.json](protocol.json)；范围调整分别见[cost14协议](cost14/protocol.json)及[native2026协议](native2026/protocol.json)。调整由数据不可得触发，发生在对应新结果分析前，未更换候选阈值或选优标准。',
        '- [18个月基线诊断](baseline18-diagnostics.json)、[成本研究结果](cost14/fixed-results.json)、[退出研究结果](native2026/fixed-results.json)、两个子目录中的selection-before-test.json、fold-results.json、walkforward-results.json及attribution.json提供完整对应证据。',
        '- 两个研究各32个B2/B5控制（2名单×2版本×2政策×4执行场景）复核通过；成本20%的2026可占用个体逐项匹配新原生引擎。接受顺序、月度/品种合计与生产文件哈希核验见各verification.json。',
        '- 原生执行器沿用冻结库，仅调用层允许固定的第5/第8桶参数；构建时3个成本边界测试通过。完整18个月新退出回放尚未完成，失败连接没有生成行情结果或填入零盈亏。',
        '', '![月度贡献、候选收益及滚动差额](comparison.png)','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8')
    chart(b,scopes)
    verify_and_finish(scopes)


def chart(b,scopes):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','SimHei','DejaVu Sans'],'axes.unicode_minus':False})
    fig=plt.figure(figsize=(13,12),layout='constrained',facecolor='#f8fafc')
    gs=fig.add_gridspec(3,1,height_ratios=[1.15,1,1])
    ax=fig.add_subplot(gs[0]);months=s.MONTHS
    values=np.array([[b['products'][sym]['warning_month_net'][mo] for mo in months] for sym in s.FOUR])
    # July JD has no source bars; distinguish absent data from covered zero PnL.
    values[s.FOUR.index('JD0'),months.index('2026-07')]=np.nan
    vmax=max(abs(np.nanmin(values)),abs(np.nanmax(values)))
    cmap=plt.get_cmap('RdYlGn').copy();cmap.set_bad('#cbd5e1')
    im=ax.imshow(values,aspect='auto',cmap=cmap,vmin=-vmax,vmax=vmax)
    ax.set_yticks(range(4),['EG 乙二醇','PVC','JD 鸡蛋','SR 白糖'])
    ax.set_xticks(range(len(months)),[mo[2:] for mo in months],rotation=45,ha='right')
    for i in range(4):
        for j in range(len(months)):ax.text(j,i,'缺数据' if np.isnan(values[i,j]) else f'{values[i,j]:.0f}',ha='center',va='center',fontsize=7,color='#0f172a')
    ax.set_title('原B5：18个月各品种预警月净额 / 元（单手；近期覆盖不齐）',loc='left',pad=12)
    fig.colorbar(im,ax=ax,shrink=.65,pad=.015)
    ax=fig.add_subplot(gs[1]);x=np.arange(4);fixed=scopes['native2026']['fixed']
    for i,(name,color) in enumerate([('B5','#64748b'),('JD20','#14b8a6'),('AG_S1','#2563eb')]):
        vals=[fixed['four/original/'+s.m.PRIMARY+'/'+name]['products'][sym]['net'] for sym in s.FOUR]
        ax.bar(x+(i-1)*.24,vals,.24,label=s.LABELS[name],color=color)
    ax.set_xticks(x,['EG','PVC','JD','SR']);ax.axhline(0,color='#475569',lw=.7)
    ax.set_title('固定候选2026收益：S1增加农产品贡献，EG仍占主导',loc='left',pad=12)
    ax.set_ylabel('已知平仓净额 / 元');ax.legend(frameon=False)
    ax=fig.add_subplot(gs[2]);fold=scopes['native2026']['folds']['four/original/'+s.m.PRIMARY]
    vals=[f['selected']['net']-f['B5']['net'] for f in fold]
    bars=ax.bar(range(len(fold)),vals,color=['#64748b' if abs(v)<1e-8 else '#0d9488' if v>0 else '#dc2626' for v in vals],width=.5)
    ax.set_xticks(range(len(fold)),['2026 4—6月','2026 7—9月','2026 10月（部分）'])
    for rect,f,v in zip(bars,fold,vals):
        ax.annotate(f"{v:+.2f}元\n{s.LABELS[f['variant']]}",(rect.get_x()+rect.get_width()/2,v),ha='center',va='bottom',xytext=(0,8),textcoords='offset points',fontsize=10)
    ax.set_ylim(min(-20,min(vals)*1.3),max(130,max(vals)*1.7));ax.axhline(0,color='#475569',lw=.8)
    ax.set_ylabel('相对B5净额差 / 元');ax.set_title('只用此前结果选规则：退出研究后续组的改善很小',loc='left',pad=12)
    for ax in fig.axes[2:]:
        ax.spines[['top','right']].set_visible(False)
    fig.suptitle('少品种专精研究 · 保留收益来源，检验真实增量',fontsize=18,fontweight='bold')
    fig.savefig(RUN/'comparison.png',dpi=165);plt.close(fig)


def verify_and_finish(scopes):
    p=read(RUN/'protocol.json')
    checks={}
    for scope,a in scopes.items():
        v=read(RUN/scope/'verification.json')
        assert v['exact_controls']==32 and not v['production_hash_differences']
        cases=0
        for z in list(a['fixed'].values())+list(a['wf'].values()):
            assert abs(sum(z['warning_month_net'].values())-z['net'])<1e-6
            assert abs(sum(q['net'] for q in z['products'].values())-z['net'])<1e-6
            assert sum(q['n'] for q in z['products'].values())==z['n']
            cases+=1
        checks[scope]=dict(exact_controls=32,monthly_product_checks=cases)
    differences=[q for q,h in p['production_before'].items() if s.m.old.sha(s.ROOT/q)!=h]
    assert not differences
    frozen={}
    for root in [s.m.old.SNAP,s.ROOT/'research-strategies/best-a-portfolio-observation-v1']:
        manifest=read(root/'manifest.json')
        bad=[name for name,h in manifest.items() if s.m.old.sha(root/name)!=h]
        assert not bad,bad
        frozen[str(root)]=dict(files=len(manifest),exact=True)
    write('final-verification.json',dict(checks=checks,source_manifests=frozen,production_hash_differences=differences,
          full18_new_exits_complete=False,coverage='18months baseline;14months cost gates;2026 only alternate exits',
          raw_minutes_reacquisition='SDK initialization and WebSocket handshake failed; no new2025 raw prices received',
          independent_untouched_oos=False))
    if (RUN/'completion.json').exists():
        previous=RUN/('completion-revision-%02d.json' % (1+len(list(RUN.glob('completion-revision-*.json')))))
        (RUN/'completion.json').rename(previous)
    write('completion.json',dict(status='available_data_study_complete',full18_new_exits_complete=False,
          created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
          production_changed=False,qualified_for_trading=False,
          artifacts={str(q.relative_to(RUN)):s.m.old.sha(q) for q in RUN.rglob('*') if q.is_file() and 'fork' not in q.relative_to(RUN).parts}))
    print('Report, chart, baseline reconciliation and64 baseline controls verified.',flush=True)


if __name__=='__main__':report()
