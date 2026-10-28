"""Explain causal simple benchmarks separately from conditional timing nulls."""
import collections
import datetime as dt
import json
import random
import statistics as st
import research_swing_incremental_edge as r

RUN=r.RUN
READ=r.READ
LABELS={'B5_original':'原B5完整规则','ABC_common':'ABC信号＋统一ATR规则','MOM16':'16桶动量＋统一ATR规则',
        'BREAK20':'20桶突破＋统一ATR规则','simple_walkforward':'此前训练选定的简单基准'}

def write(name,z):r.s.m.old.write(RUN/name,z)
def fmt(v):return '—' if v is None else f'{v:,.2f}'
def pctile(xs,x):return 100*sum(v<x for v in xs)/len(xs)
def quantile(xs,p):
    a=sorted(xs);return a[round((len(a)-1)*p)]

def summarize_null(control,samples):
    return dict(repetitions=len(samples),normalized_net=control['normalized_net'],
                null_normalized_net_median=st.median(z['normalized_net'] for z in samples),
                null_normalized_net_05=quantile([z['normalized_net'] for z in samples],.05),
                null_normalized_net_95=quantile([z['normalized_net'] for z in samples],.95),
                normalized_net_percentile=pctile([z['normalized_net'] for z in samples],control['normalized_net']),
                mean_nominal_net_r=control['mean_nominal_net_r'],
                null_mean_nominal_net_r_median=st.median(z['mean_nominal_net_r'] for z in samples),
                mean_nominal_r_percentile=pctile([z['mean_nominal_net_r'] for z in samples],control['mean_nominal_net_r']),
                closed_n=control['n'],null_closed_n_median=st.median(z['n'] for z in samples),
                unknown=control['unknown'],null_unknown_min=min(z['unknown'] for z in samples),null_unknown_max=max(z['unknown'] for z in samples))

def block_interval(values):
    rng=random.Random(2026102633);xs=[];n=len(values)
    for _ in range(4000):
        sample=[]
        while len(sample)<n:
            j=rng.randrange(n);sample.extend(values[(j+k)%n] for k in range(3))
        xs.append(sum(sample[:n]))
    return dict(months=n,block_months=3,replications=4000,observed=sum(values),interval95=[quantile(xs,.025),quantile(xs,.975)],
                interpretation='Descriptive paired monthly block interval only.7 partly covered months, historical product/strategy selection and unmatched unknown PnL remain. Not selection-adjusted significance.')

def run():
    fixed=READ(RUN/'fixed-results.json');test=READ(RUN/'test-results.json');folds=READ(RUN/'fold-results.json')
    null=READ(RUN/'matched-null-results.json');candidate=READ(RUN/'candidate-null-results.json')
    plan=READ(RUN/'plan-preserving-null/results.json');plancandidate=READ(RUN/'plan-preserving-null/candidate-results.json')
    schedule=READ(RUN/'walkforward-selection.json')['schedule'];meta=READ(RUN/'clock-metadata.json')
    primary='original/'+r.PRIMARY;summary={};uncertainty={}
    for case in test:
        control=plan[case]['-1'];refs=[z for rep,z in plan[case].items() if rep!='-1']
        # Plan-preserving original-clock controller is exactly the original B5.
        for field in ['n','net','normalized_net','mean_net_r','unknown','warning_month_normalized_net']:
            assert r.s.m.equal(control[field],test[case]['B5_original'][field]),(case,field)
        summary[case]=dict(common_ATR=summarize_null(test[case]['ABC_common'],null[case]),
            common_ATR_individual=summarize_null(candidate[case]['ABC'],candidate[case]['null']),
            plan_preserving=summarize_null(control,refs),
            plan_preserving_individual=summarize_null(plancandidate[case]['-1'],[z for rep,z in plancandidate[case].items() if rep!='-1']),
            products={sym:summarize_null(control['products'][sym],[z['products'][sym] for z in refs]) for sym in r.SYMS})
        uncertainty[case]={}
        for other in ['simple_walkforward','ABC_common']:
            a=test[case]['B5_original']['warning_month_normalized_net'];b=test[case][other]['warning_month_normalized_net']
            uncertainty[case][other]=block_interval([a[mo]-b[mo] for mo in a])
    write('comparison-summary.json',summary);write('uncertainty.json',uncertainty)
    # Compare signal-state strata with predeclared16bar direction efficiency>=.30.
    groups=collections.defaultdict(list)
    for line in (RUN/'accepted-primary.jsonl').open(encoding='utf-8'):
        z=json.loads(line);row=z['row']
        if row['warning_ts']<r.FOLDS[0][0] or r.EV(row)['status']!='closed':continue
        key=row['event_key'] if row['event_key'] in meta else '|'.join(['CLOCK',row['symbol'],row['direction'],row['warning_ts']])
        reg=meta[key]['regime'] if key in meta else 'unavailable'
        groups[z['model'],reg].append(row)
    regime={model:{reg:r.metric(rs,meta,[mo for mo in r.MONTHS if mo>='2026-04']) for (m,reg),rs in groups.items() if m==model} for model in LABELS}
    write('regime-results.json',regime)
    # Matching support and overlap are diagnostic, not replicate selection criteria.
    m2={k:dict(z,stratum=r.group(z)) for k,z in meta.items()}
    pops=collections.Counter(z['stratum'] for z in m2.values());cnt=collections.Counter(z['stratum'] for z in m2.values() if z['ABC_common'])
    support=dict(abc_warning_n=sum(cnt.values()),strata=len(cnt),
                 locked_warning_n=sum(n for g,n in cnt.items() if pops[g]==n),
                 expected_ABC_clock_overlap=sum(n*n/pops[g] for g,n in cnt.items()),
                 smallest_population=min(pops[g] for g in cnt),largest_population=max(pops[g] for g in cnt))
    write('matching-support.json',support)
    report(fixed,test,folds,summary,schedule,regime,uncertainty,support)
    chart(test,null,plan,summary)
    finish(summary)
    print(json.dumps(dict(plan_summary=summary[primary]['plan_preserving'],
          common_summary=summary[primary]['common_ATR'],products=summary[primary]['products']),ensure_ascii=False),flush=True)

def report(fixed,test,folds,summary,schedule,regime,uncertainty,support):
    primary='original/'+r.PRIMARY;z=test[primary];sn=summary[primary]
    lines=['# ABC是否提供额外价值：简单策略与随机时点对照','',
        '日期：2026-10-26（北京时间）。主组合固定EG/PVC/JD/SR。沿用已有2026行情和冻结费率，没有下载新行情、修改原B5或连接实盘。2026年1—3月为简单基准初始训练，4—6月、7—9月、10月已有覆盖为后续计分组；10月截至23日11:05，部分品种7—8月覆盖不完整。',
        '', '## 1. 结论先行','',
        '**本批结果支持“原时点与结构计划的配合可能有价值”，不支持“ABC预警标签本身就是稳定优势”，也未完成独立前瞻验证。**',
        '', '原B5在4—10月主场景105笔、净额7,443.12元，优于两套事先固定的日内动量/突破基准。若把ABC信号换成统一ATR止损、2R目标和统一触价执行，结果反而亏损，且没有优于匹配随机时点。这个区别意味着原结构目标、止损、经济性及小时执行不能随意移除，也不能用“有ABC形态就有优势”概括当前证据。',
        '', '随后保留原计划的止损/目标距离、入场缓冲和执行指令，做199组匹配随机时点诊断。原时点组合主场景风险归一净额处于约98.0百分位；最严成交压力下降至70.4百分位，单笔收益排名也更弱。它是有条件的历史支持，不能称稳健、独立或经过选择偏差校正的形态alpha。',
        '', '品种上，测试段证据主要来自EG和PVC；鸡蛋8笔平均净R约0.007，白糖17笔约0.030。两者目前不足以充当稳定的非化工利润来源，提高频率不是首要验证目标。',
        '', '## 2. 两层检验为何必须分开','',
        '|层次|保持什么|改变什么|能回答什么|', '|---|---|---|---|',
        '|原B5与简单策略|行情、费率、成交压力、单品种占用、计划风险计量|完整策略的信号和止损/目标构造|本批两个固定简单日内基准能否替代原B5；不能推广为所有简单策略均不如B5|',
        '|统一执行器比较|所有模型都用同一触发、1ATR止损、2R目标、无跟随和持仓期限|仅信号出现时刻与方向|ABC信号脱离原几何和小时适配后，有没有独立入场信息|',
        '|统一ATR随机时点|ABC候选的品种、月份、多空、时刻、16桶动量符号和粗止损宽度分组|同组内具体日期|统一执行下，ABC精确时点是否优于匹配时点；不保留原B5计划|',
        '|原计划随机时点|原计划止损/目标距离、入场缓冲、预警类型与执行指令、源A簇标签及匹配条件|同组内具体日期；新价位重新检查成本与净RR|原计划与实际时点/结构是否匹配；不是独立预测ABC形态的纯净实验|',
        '', '统一ATR层在原协议中预先冻结。原计划层是在查看第一层结果后，为避免把“换了止损/目标”误判成“原策略无效”而另立协议；没有改写第一层候选、结果或阈值，也没有反复扫描参数。这是顺序展开的机制研究，不是整批事先注册的独立验证。',
        '', '## 3. 基准、执行与风险口径','',
        '- MOM16：每根共同有效且已完成15分钟柱，方向取当前收盘相对16根之前收盘的符号；零则不发信号。',
        '- BREAK20：当前收盘突破此前20根最高价做多、跌破此前20根最低价做空；当前柱不进入此前窗口。',
        '- ABC共同执行版：既有未合并、评分≥2.5且A方向净实体为正的预警，接入共同时间网格；没有用未来是否触发、成交或盈利筛选预警。',
        '- 共同执行：信号柱极值外1tick待触发，1ATR14止损（向外取整）、2计划R绝对目标；第5桶无跟随、最多60桶持仓、12桶待触发，保留原假期/周末/缺失/追价/净RR/实际风险守卫，统一取消小时适配与行业/A簇新增控制。',
        '- 共同计划门槛仍为止损至少10tick、名义摩擦≤0.2R、成本/目标≤10%、计划与实际净RR≥1。计划成本固定1tick，不因执行压力反向调整入场资格。所有价格与指标只读当时已完成历史，窗口不跨实际合约换月。',
        '- 所有策略仍逐机会模拟一手。主要风险比较另以q＝300／（计划止损距离×乘数）缩放每笔净额，q在预警时可知。这是允许分数手的研究归一化，**不是实际下单、保证金账户回报或年化**。一品种一个占用，最多四个，归一计划占用上限1,200元；实际成交偏差及跳空可能超过计划风险。',
        '- 最大回撤为按退出顺序的已实现归一回撤，不含全部持仓浮动权益。未知损益未填零，未知敞口仍按冻结政策占用。',
        '', '## 4. 后续测试段：固定与滚动简单基准','',
        '|策略|已知平仓|一手已知净额/元|平均实际净R|300元计划风险归一净额|归一已实现回撤|未知占用|', '|---|---:|---:|---:|---:|---:|---:|']
    for model in ['B5_original','ABC_common','MOM16','BREAK20','simple_walkforward']:
        q=z[model]
        lines.append(f"|{LABELS[model]}|{q['n']}|{fmt(q['net'])}|{q['mean_net_r']:.3f}|{fmt(q['normalized_net'])}|{fmt(q['normalized_realized_dd'])}|{q['unknown']}|")
    lines += ['', '简单滚动基准每组只用此前严格已退出的影子交易选MOM16或BREAK20，评分为8场景最小归一净额−0.25×最大归一回撤，未在整段结果中挑赢家。三个测试组均选BREAK20。初始Q1按预定MOM16运行，跨组不清仓、不重置占用，所以实际滚动结果可以与从头运行的固定BREAK20不同。',
              '', '|测试组|此前选定简单基准|原B5归一净额|ABC统一ATR归一净额|简单滚动基准归一净额|', '|---|---|---:|---:|---:|']
    for i,f in enumerate(schedule):
        lines.append(f"|{f['start'][:7]}至{f['end'][:7]}（右端不含）|{f['model']}|{fmt(folds[primary]['B5_original'][i]['metrics']['normalized_net'])}|{fmt(folds[primary]['ABC_common'][i]['metrics']['normalized_net'])}|{fmt(folds[primary]['simple_walkforward'][i]['metrics']['normalized_net'])}|")
    lines+=['', '这些结果否定的是“本批所有顺势入场都会自然盈利”的简单解释，不能据此宣布B5胜过任何动量、日线持有或其他突破系统。本轮只运行上述两个固定日内定义，没有扫描窗口、ATR倍数或持仓期限，也未运行可比的日线长期持有系统。',
        '', '### 4.1 四成交压力与两种未知占用政策','', '|政策/场景|原B5归一净额|ABC统一ATR|动量MOM16|突破BREAK20|简单滚动基准|原B5/滚动未知|', '|---|---:|---:|---:|---:|---:|---:|']
    for case,models in test.items():
        lines.append('|'+case+'|'+'|'.join(fmt(models[m]['normalized_net']) for m in ['B5_original','ABC_common','MOM16','BREAK20','simple_walkforward'])+f"|{models['B5_original']['unknown']} / {models['simple_walkforward']['unknown']}|")
    ci=uncertainty[primary]['simple_walkforward']
    lines+=['',f"原B5相对简单滚动基准的月度归一增量为{fmt(ci['observed'])}元，3个月配对块重采样描述区间约[{fmt(ci['interval95'][0])}, {fmt(ci['interval95'][1])}]。只有7个部分覆盖月份，且简单基准有未知损益，区间不能提供选择偏差校正后的显著性或账户收益保证。",'',
        '## 5. 随机时点：先看统一ATR层','',
        '199组随机候选严格匹配品种、月份、方向、HH:MM、此前16桶动量符号，以及同品种计划ATR风险宽度的2次幂粗分组。数量匹配所有预警候选，不匹配最后接受或盈利交易；同一组内无放回抽样，然后每组重新竞争占用。',
        '', '|成交场景（original）|ABC归一净额|随机中位归一净额|组合净额百分位|ABC平均计划净R|随机平均计划净R中位|单笔百分位|', '|---|---:|---:|---:|---:|---:|---:|']
    for sf in r.s.m.SUFFIXES:
        q=summary['original/'+sf]['common_ATR']
        lines.append(f"|{sf}|{fmt(q['normalized_net'])}|{fmt(q['null_normalized_net_median'])}|{q['normalized_net_percentile']:.1f}|{q['mean_nominal_net_r']:.3f}|{q['null_mean_nominal_net_r_median']:.3f}|{q['mean_nominal_r_percentile']:.1f}|")
    lines+=['', '统一ATR下，ABC没有显示精确时点优势。组合总额排名比单笔排名更差，一部分与不同触发/接受数量和费用有关，因此不能只看总额判定信号质量。',
        '', '## 6. 保留原计划距离与执行指令后的随机时点','',
        '从冻结预算/净实体版本中只读取预警与计划字段，保留363个通过原几何/成本/净实体门槛且有共同时间网格的候选；没有用最终成交、退出、MFE或盈亏选候选。每个原计划另随机移动到匹配日期，保留止损/目标距离、入场缓冲、预警类型和小时执行指令，新价格重新经过计划/实际费用守卫。原时点个体回放1,452行逐项匹配冻结结果，组合在全部8场景也精确匹配原B5测试结果。',
        '', '|成交场景（original）|原时点归一净额|随机中位|随机5%—95%区间|组合百分位|单笔百分位|原时点/随机中位平仓数|', '|---|---:|---:|---:|---:|---:|---:|']
    for sf in r.s.m.SUFFIXES:
        q=summary['original/'+sf]['plan_preserving']
        lines.append(f"|{sf}|{fmt(q['normalized_net'])}|{fmt(q['null_normalized_net_median'])}|{fmt(q['null_normalized_net_05'])} 至 {fmt(q['null_normalized_net_95'])}|{q['normalized_net_percentile']:.1f}|{q['mean_nominal_r_percentile']:.1f}|{q['closed_n']} / {q['null_closed_n_median']:.0f}|")
    q=sn['plan_preserving_individual']
    lines+=['',f"不处理重叠、仅作单机会诊断时，主场景原时点平均计划净R为{q['mean_nominal_net_r']:.3f}，随机中位约{q['null_mean_nominal_net_r_median']:.3f}。这些重叠机会不能直接相加为可执行组合收益，但差异说明主场景组合优势并非仅由接受笔数增加解释。",
        '', '**百分位不是置信度，也不是独立样本外p值。** 随机对照保留了ABC的方向和月份分布，检验的是额外时点/结构配合，不能再用它证明方向预测能力。原计划及小时执行指令来自已观察ABC预警，可能被搬到同月更早日期，A簇也只保留为源标签；这是一项条件随机化诊断，无法在当时真实执行。粗分组不能控制所有行情状态，随机组合还有0—3笔未知占用，不能将其损益当零。',
        '',f"匹配支持：{support['abc_warning_n']}个ABC预警分布在{support['strata']}个分组，{support['locked_warning_n']}个因组内没有额外时间位置而被锁定；随机与原ABC时点的期望重叠约{support['expected_ABC_clock_overlap']:.1f}个。实际ABC时点保留在随机总体中，未通过排除原时点来人为拉大差异。",
        '', '## 7. 哪些品种支持这套组合规则','',
        '|品种|测试平仓|一手净额/元|平均实际净R|风险归一净额|保留计划随机中位|主场景百分位|四压力百分位范围（original）|', '|---|---:|---:|---:|---:|---:|---:|---:|']
    for sym in r.SYMS:
        a=z['B5_original']['products'][sym];b=sn['products'][sym]
        ps=[summary['original/'+sf]['products'][sym]['normalized_net_percentile'] for sf in r.s.m.SUFFIXES]
        lines.append(f"|{r.s.m.NAMES[sym]}|{a['n']}|{fmt(a['net'])}|{a['mean_net_r']:.3f}|{fmt(a['normalized_net'])}|{fmt(b['null_normalized_net_median'])}|{b['normalized_net_percentile']:.1f}|{min(ps):.1f}—{max(ps):.1f}|")
    lines+=['', 'PVC在本段的表现和保留计划对照优于此前18个月合计给人的薄弱印象，说明不同时间段适配性有变化。它也提示不能把全部正结果都解释成EG长期上涨；但33笔样本和同历史筛选仍不足以宣布跨期稳定。',
        '', '鸡蛋在这一测试段的单笔质量也接近零，已经不能只用“频率太少”解释。白糖主场景略正，但最严压力显著变差。它们不应为凑非化工利润来源而统一放宽，新的策略机制需要另立证据。',
        '', '### 7.1 同一预警时可见的短期行情状态','', '|策略|16桶状态|已知平仓|平均实际净R|归一净额|', '|---|---|---:|---:|---:|']
    for model in ['B5_original','ABC_common','MOM16','BREAK20']:
        for reg,a in regime[model].items():lines.append(f"|{LABELS[model]}|{reg}|{a['n']}|{a['mean_net_r']:.3f}|{fmt(a['normalized_net'])}|")
    lines+=['', '状态来自预警时已完成16根15分钟柱的方向效率，效率≥0.30分向上/向下，其余为range；它是固定的描述标签，不是日线牛熊分类或新交易门槛。跨品种/状态分组样本很小，不能再从表中挑一个最优状态当作已经验证的新策略。',
        '', '## 8. 对研究方向的调整','',
        '1. 保留原B5完整版本作研究基线；本轮不建议把ABC信号改成通用ATR止损/2R目标，也不将“形态预警本身有优势”当成默认前提。',
        '2. 当前有条件支持集中在EG/PVC，优先检验原结构计划与时点配合能否在新行情延续；它们仍有化工共同风险。按计划现金风险分配值得单独验证，但本轮分数手归一值不能直接变成下单手数。',
        '3. JD/SR暂停以增加交易频率为首要目标。尤其JD此前18个月平均净R较好，在后续段并未延续；应先寻找其独立、可在时点上定义的优势，再决定是否放宽。未修改任何实际名单或生产参数。',
        '4. 不再凭这一批历史继续堆叠精细过滤。后续最有价值的是冻结当前定义后的新行情验证，以及另行预注册日线暴露/低换手基准；本轮日内基准和条件随机化不能替代真正独立前瞻。',
        '', '## 9. 校验、范围与证据入口','',
        '共完成406,496行新的个体执行评估（统一执行116,096；原计划随机化290,400），它们共享同一历史，不是406,496个独立市场样本。原B5的8场景控制通过；原计划层1,452行原时点个体严格一致，8个组合控制也一致。所有接受顺序、严格退出后释放占用、费用与价格tick、训练截点以及生产文件哈希通过核验。',
        '', '原B5本身、品种名单及部分假设此前已在这些历史开发。按组训练/测试只保证本轮简单基准选择不读取后续结果；不能使旧策略和数据恢复成完全未见样本。2025新退出分钟数据未补齐，本轮没有声称覆盖完整18个月。',
        '', '- [初始协议](protocol.json)、[随机匹配风险分组细化](null-design-amendment.json)、[原计划跟进协议](plan-preserving-null/protocol.json)。',
        '- [简单基准逐组选择](walkforward-selection.json)、[测试结果](test-results.json)、[每组结果](fold-results.json)、[随机比较摘要](comparison-summary.json)。',
        '- [统一ATR随机结果](matched-null-results.json)、[原计划随机结果](plan-preserving-null/results.json)、[单机会原计划结果](plan-preserving-null/candidate-results.json)。',
        '- [首层校验](verification.json)、[原计划校验](plan-preserving-null/verification.json)、[最终校验](final-verification.json)。',
        '', '![不同策略、两类随机对照及品种贡献](comparison.png)','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8')

def chart(test,null,plan,summary):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','SimHei','DejaVu Sans'],'axes.unicode_minus':False})
    fig,axes=plt.subplots(2,2,figsize=(13,10),layout='constrained',facecolor='#f8fafc');case='original/'+r.PRIMARY
    ax=axes[0,0];models=['B5_original','ABC_common','MOM16','BREAK20'];values=[test[case][m]['normalized_net'] for m in models]
    bs=ax.bar(range(4),values,color=['#0d9488','#dc2626','#ef4444','#f97316'])
    ax.bar_label(bs,fmt='%.0f',padding=3);ax.axhline(0,color='#475569',lw=.8);ax.set_xticks(range(4),['原B5','ABC＋ATR','动量16','突破20'])
    ax.set_ylabel('每笔计划风险300元的归一净额');ax.set_title('4—10月已知平仓：完整规则与信号层表现不同',loc='left',fontsize=11)
    ax=axes[0,1];xs=[z['mean_nominal_net_r'] for z in null[case]];actual=test[case]['ABC_common']['mean_nominal_net_r']
    ax.hist(xs,bins=22,color='#94a3b8',alpha=.9);ax.axvline(actual,color='#dc2626',lw=2,label=f'ABC {actual:.3f}R')
    ax.set_title('统一ATR：ABC平均单笔表现未胜随机时点',loc='left',fontsize=11);ax.set_xlabel('平均计划净R');ax.set_ylabel('随机组合数');ax.legend(frameon=False)
    ax=axes[1,0];xs=[z['normalized_net'] for rep,z in plan[case].items() if rep!='-1'];actual=plan[case]['-1']['normalized_net']
    ax.hist(xs,bins=22,color='#94a3b8',alpha=.9);ax.axvline(actual,color='#0d9488',lw=2,label=f'原B5 {actual:.0f}元')
    ax.set_title('保留原计划：原时点组合约处98百分位',loc='left',fontsize=11);ax.set_xlabel('归一已知平仓净额 / 元');ax.set_ylabel('随机组合数');ax.legend(frameon=False)
    ax=axes[1,1];x=np.arange(4);actual=[test[case]['B5_original']['products'][s]['normalized_net'] for s in r.SYMS]
    median=[summary[case]['products'][s]['null_normalized_net_median'] for s in r.SYMS]
    ax.bar(x-.18,actual,.35,color='#0d9488',label='原B5');ax.bar(x+.18,median,.35,color='#94a3b8',label='原计划随机中位')
    ax.set_xticks(x,['EG','PVC','JD','SR']);ax.axhline(0,color='#475569',lw=.8);ax.set_ylabel('归一已知平仓净额 / 元');ax.set_title('证据主要集中于EG/PVC，非化工仍弱',loc='left',fontsize=11);ax.legend(frameon=False)
    for ax in axes.flat:ax.grid(axis='y',alpha=.14);ax.spines[['top','right']].set_visible(False)
    fig.suptitle('ABC增量价值检验 · 条件历史支持，尚非独立验证',fontweight='bold',fontsize=17)
    fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)

def finish(summary):
    p=READ(RUN/'protocol.json');v=READ(RUN/'verification.json');pv=READ(RUN/'plan-preserving-null/verification.json')
    assert len(v['original_B5_controls'])==8 and pv['counts']['exact_original_clock_individuals']==1452
    qa=collections.Counter()
    for filename in ['fixed-results.json','test-results.json']:
        for case,models in READ(RUN/filename).items():
            for name,z in models.items():
                assert abs(sum(q['net'] for q in z['products'].values())-z['net'])<1e-6
                assert abs(sum(q['normalized_net'] for q in z['products'].values())-z['normalized_net'])<1e-6
                assert abs(sum(z['warning_month_normalized_net'].values())-z['normalized_net'])<1e-6
                assert sum(q['n'] for q in z['products'].values())==z['n']
                qa['product_month_reconciliation']+=1
    for filename in [RUN/'matched-null-results.json',RUN/'plan-preserving-null/results.json']:
        data=READ(filename)
        for cases in data.values():
            zs=cases.values() if isinstance(cases,dict) else cases
            for z in zs:
                assert abs(sum(q['normalized_net'] for q in z['products'].values())-z['normalized_net'])<1e-6
                assert abs(sum(z['warning_month_normalized_net'].values())-z['normalized_net'])<1e-6
                qa['random_product_month_reconciliation']+=1
    source=READ(RUN/'generation-checks.json')['source_hashes'];source_diff=[]
    pp=READ(r.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')
    for period,hashes in source.items():
        for key,h in hashes.items():
            if r.s.m.old.sha(pp['periods'][period][key])!=h:source_diff.append([period,key])
    assert not source_diff
    production_diff=[q for q,h in p['production_before'].items() if r.s.m.old.sha(r.ROOT/q)!=h];assert not production_diff
    manifests={}
    for root in [r.s.m.old.SNAP,r.ROOT/'research-strategies/best-a-portfolio-observation-v1']:
        bad=[q for q,h in READ(root/'manifest.json').items() if r.s.m.old.sha(root/q)!=h];assert not bad
        manifests[str(root)]=dict(exact=True,files=len(READ(root/'manifest.json')))
    n=sum(READ(RUN/period/'executions/manifest.json')['evaluations'] for period in ['early','recent'])
    n+=sum(READ(RUN/'plan-preserving-null'/period/'executions/manifest.json')['evaluations'] for period in ['early','recent'])
    assert n==406496,n
    write('final-verification.json',dict(checks=dict(qa),new_individual_evaluations=n,original_B5_scenarios_exact=8,
        plan_preserving_individual_controls_exact=1452,plan_preserving_portfolio_controls_exact=8,
        source_hash_differences=source_diff,production_hash_differences=production_diff,source_manifests=manifests,
        independent_untouched_OOS=False,matched_null_is_causal_strategy=False,production_changed=False))
    write('completion.json',dict(status='complete',created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        production_changed=False,qualified_for_trading=False,
        script_hashes={str(r.ROOT/'scripts'/name):r.s.m.old.sha(r.ROOT/'scripts'/name) for name in ['research_swing_incremental_edge.py','research_swing_plan_timing_null.py','report_swing_incremental_edge.py']},
        artifacts={str(q.relative_to(RUN)):r.s.m.old.sha(q) for q in RUN.rglob('*') if q.is_file()}))

if __name__=='__main__':run()
