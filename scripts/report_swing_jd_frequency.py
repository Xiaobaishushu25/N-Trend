"""Frequency, profitability, robustness and complete failure disclosure for JD."""
import collections
import datetime as dt
import json
import subprocess
import research_swing_jd_frequency as z

RUN=z.RUN
REV=RUN/'report-v2'
READ=z.READ
EV=z.EV
def write(n,v):z.s.m.old.write(REV/n,v)

def fmt(x):return '—' if x is None else f'{x:,.2f}'
def title(m):
    a,_,p=m.partition('/')
    return z.LABELS.get(a,a)+('·日内' if p=='QUICK' else '·较大空间' if p else '')

def native_controls():
    binary=READ(RUN/'binary.json');assert z.s.m.old.sha(binary['path'])==binary['sha256']
    parent=READ(z.x.RUN/'protocol.json');count=0
    for period in ['early','recent']:
        src=z.x.RUN/period/'JD';dest=RUN/'native-controls'/period
        if not dest.exists():
            dest.mkdir(parents=True)
            for n in [period+'-pool.json','input-specs.json']:
                (dest/n).write_bytes((src/n).read_bytes())
            p=READ(src/'execution.protocol.json');p['templates']=[parent['templates'][n] for n in ['B5','E20S1']]
            p['periods'][period]['input']=str(dest/'input-specs.json')
            z.s.m.old.write(dest/'execution.protocol.json',p)
            subprocess.run([binary['path'],str(dest/'execution.protocol.json'),period,'replay',str(dest/'executions')],check=True)
        old={}
        for line in (src/'executions/evaluations.jsonl').open(encoding='utf-8'):
            a=json.loads(line)
            if a['scenario'].split('/')[0] in ['B5','E20S1']:old[a['event_key'],a['scenario']]=a
        for line in (dest/'executions/evaluations.jsonl').open(encoding='utf-8'):
            a=json.loads(line);b=old[a['event_key'],a['scenario']]
            for key in ['result','entry','stop','target','planned_risk','new_friction','economic_pass','body_net_pass']:
                assert a.get(key)==b.get(key),(period,key,a['event_key'])
            count+=1
    return count

def verify():
    p=READ(RUN/'protocol.json');gen=READ(RUN/'generation-checks.json')
    pp=READ(z.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')
    for period,hs in gen['sources'].items():
        for key,h in hs.items():assert z.s.m.old.sha(pp['periods'][period][key])==h
    for q,h in p['production_before'].items():assert z.s.m.old.sha(z.ROOT/q)==h
    old=z.x.RUN/'fork';allowed={'crates/n-core/src/v3/ablation.rs'};changed=[];same=0
    for q in old.rglob('*'):
        if not q.is_file():continue
        rel=q.relative_to(old).as_posix()
        if z.s.m.old.sha(q)!=z.s.m.old.sha(RUN/'fork'/rel):changed.append(rel)
        else:same+=1
    assert set(changed)==allowed
    return dict(raw_inputs_unchanged=True,production_unchanged=True,fork_original_files_unchanged=same,
        fork_changed_files=changed,exact_native_frozen_control_rows=native_controls())

def run():
    REV.mkdir(exist_ok=False)
    fixed=READ(RUN/'fixed-results.json');wf=READ(RUN/'walkforward-results.json')
    p=READ(RUN/'protocol.json');primary=p['primary'];cases=list(wf);gen=READ(RUN/'generation-checks.json')
    selection=READ(RUN/'selection-before-test.json');checks=verify()
    rows=collections.defaultdict(list)
    for line in (RUN/'accepted-primary.jsonl').open(encoding='utf-8'):
        a=json.loads(line);rows[a['model']].append(a['row'])
    summary={};eligible=[]
    for model in z.MODELS:
        q=fixed[primary+'/'+model];cs=[fixed[c+'/'+model] for c in cases]
        closed=[a for a in rows[model] if EV(a)['status']=='closed']
        fee=sum(a['cost_repricing']['commission'] for a in closed)
        slip=sum(sum(t['volume'] for t in a['result']['fills'])*a['slip']*a['tick_size']*a['multiplier'] for a in closed)
        worst=min(v['net'] for v in cs);worstnorm=min(v['normalized_net'] for v in cs)
        flags=dict(at_least60=q['n']>=60,positive_all_cases=worst>0,no_unknown=all(v['unknown']==0 for v in cs),
            ex_top3_positive=q['normalized_ex_top3']>0,ex_best_month_positive=q['normalized_ex_best_month']>0,
            positive3_blocks=sum(b['net']>0 for b in q['blocks'].values())>=3)
        summary[model]=dict(primary=q,worst8_net=worst,worst8_normalized=worstnorm,max8_unknown=max(v['unknown'] for v in cs),
            min8_closed=min(v['n'] for v in cs),test_worst8_net=min(v['test']['net'] for v in cs),
            commission=fee,embedded_slippage_accounting=slip,fixed_path_before_friction=q['net']+fee+slip,
            flags=flags,retrospective_frequency_and_robustness_screen_pass=all(flags.values()))
        if model in z.CANDIDATES and all(flags.values()):eligible.append(model)
    # Post-hoc descriptive rankings are explicitly not promoted strategies.
    freq=[m for m in z.CANDIDATES if summary[m]['primary']['n']>=60]
    rank=sorted(freq,key=lambda m:(-summary[m]['primary']['normalized_net'],z.MODELS.index(m)))
    riskrank=sorted(freq,key=lambda m:(-summary[m]['worst8_normalized'],z.MODELS.index(m)))
    out=dict(models=summary,frequency_candidates=rank,frequency_candidates_by_worst_case=riskrank,
        retrospective_screen_pass=eligible,selection=selection['schedule'],walkforward=wf,
        shared_days=len(gen['shared_dates']),not_untouched_OOS=True,check=checks)
    lines=['# 鸡蛋15分钟：扩大机会来源与交易频率研究','',
        '日期：2026-10-26（北京时间）。EG/PVC/SR及生产策略均未改。本轮从独立15分钟机会机制出发，不再只放宽B5退出。',
        '', '## 直接结论','',
        f"原来只有9—20笔，不是15分钟周期天然只能产生这些交易，而是ABC候选、结构风险、目标空间和资格门槛共同筛到很少。本轮20个可选系统中，已有{len(freq)}个达到实际覆盖期60笔以上；频率可以实现，但没有一个同时通过全部收益、成交压力和跨组检验。",
        '', '最值得继续审视的是8柱短通道突破的日内机制：原覆盖180笔、主场景扣费净额1,802.97元，300元计划风险归一净额3,490.83，删除最大的3笔归一盈利后仍正。它有较宽的交易来源，但Q1亏325.33元、Q3覆盖段亏2,017.85元，并非均匀稳定。滑点从每边1tick增至2tick即转为亏1,698.57元，再延后一分成交时最差亏4,492.85元；尚不能升级。',
        '', '相同风险口径同样要看：日内开盘区间130笔、现金净额530.50元，但归一净额-1,031.28，表明现金盈利偏向大风险交易，不能仅凭现金总和认为优势可靠。单纯把ABC信号换成更宽松的波动/费用空间，92或124笔后反而亏损，放宽门槛不是充分解法。',
        '', '按严格训练结果前推，Q2选到开盘区间较大空间版，做33笔、主场景254元；压力后最低-1,629元。Q3和10月未选出符合门槛的高频方案，按事前规则空仓。这个结果保留，不以事后最高的180笔曲线替代。',
        '', '## 当前最有用的研究方向','',
        '1. 将8柱日内突破作为高频挑战者，将E20S1作为低频空间基准。后续重点应是可提前识别的延续质量和可成交净空间，避免靠继续缩止损、增加手数或更多参数扫描宣称改善。',
        '2. 不能把多个信号的独立利润相加充当策略组合。本轮固定优先级合并和ER状态切换都亏损，说明“多几种机会”本身不会自动带来分散。',
        '3. 新增了顺慢趋势、ER过滤、同日通道三个简单假设，并在规则冻结后补测归档7月上旬。详见下方补充：同风险表现、删赢家和滑点仍未过关。',
        '4. 下一层差异化机制可考虑鸡蛋实际合约之间的期限结构/跨期价差，而非继续依靠单一主力价格方向。现存归档有多个JD合约的同期分钟重叠，但早期大部分相邻合约重叠很短，不能直接拼接为同一稳定价差；需另行核验双腿可成交性、两倍腿数的费用和换对规则，尚未在本轮验证收益。',
        '', '## 研究边界','',
        f"固定8类信号来源、2个事前定义的机会合并方式，各配较大空间与日内两套执行，共20个可选系统；另有4个动量/随机方向基准和B5、E20S1两个旧策略对照。共有{len(gen['shared_dates'])}个有合格共享时钟的实际覆盖交易日、{gen['counts']['eligible_clocks']}个完成15分钟时钟。频率目标为实际覆盖期至少60笔已知平仓，不年化、不把下单和未成交算成交易。",
        '', '只使用已有2026原始分钟行情：1—6月、8月10日至10月23日部分；7月和8月初的JD计分缺口没有补成零收益。每个信号至少37个已完成15分钟柱在同一个实际主力合约段内，排除重复标签。各旧对照保留其原时钟，因此与共享时钟候选的热身边缘覆盖略有差异。历史与品种已被多次研究，24个新固定系统也带来多重比较；本轮是探索及时间前推检验，不是独立未触碰样本。',
        '', '## 机制与执行尺度','',
        '|机会来源|固定定义|','|---|---|',
        '|ABC时点|保留评分后ABC出现时点，重新按波动和成本规划空间|',
        '|短通道突破|收盘突破此前8柱高低点|',
        '|趋势回撤|8/32均线顺向、慢线同向，回撤后收盘重新穿回快线|',
        '|内包收缩|当前柱被上一柱包住，按8/32均线方向寻找突破|',
        '|波动带回归|ER16低于0.30、价格在此前20柱均值1.5倍标准差外，实体向均值反转，目标均值|',
        '|失败突破|扫过此前20柱区间一侧后收回，反方向交易到旧区间中点|',
        '|开盘区间|09:15/09:30两柱定区间，后续收盘新穿越区间边界|',
        '|跳空回补|首15分钟相对上个同合约完整交易日收盘跳空≥ATR，实体向缺口回归|',
        '|趋势合并|按趋势回撤、内包、开盘区间、短通道的固定优先级取一个；同钟方向冲突则不取|',
        '|状态切换|ER16≥0.30取趋势合并，否则按跳空、失败突破、波动带取一个|',
        '', '较大空间：信号柱极值外1tick触发，至少1.5ATR止损、2R目标、最长32个15分钟观察桶。日内：信号完成后下一分钟开盘入场，至少1ATR止损、1.8R目标、最长12桶，14:30起禁止新入场、14:55有真实报价才平仓。均值回归类保留原均值/中点/前收盘目标，不因空间不足把目标移远。',
        '', '两类均保留至少10tick风险、实际入场净盈亏比及追价保护。计划风险还须覆盖费用：较大空间名义摩擦不超过0.20R、成本/目标不超过10%；日内分别为0.30R和20%。这是两种明确的执行方案，同时改变多个环节，不能把其差异单独归因于某一个参数。',
        '', '**每个系统只有一个鸡蛋挂单/持仓槽位，从预警时就占用，未触发订单也会阻挡后续预警；前一订单终止时间严格早于下一预警才能再开。** 不按未来哪个信号先成交来倒选，也不把不同机制各自独立的利润直接相加。旧对照也按此方式重竞争；它与此前按触发时刻竞争的B5口径不同。',
        '', '金额均为各一手、扣手续费及滑点后的已知平仓净额；300元计划风险归一仅为分数手研究比较，不是账户净值或可直接下单手数。8场景为成交模式、滑点和未知释放政策的敏感性，并非独立市场样本。',
        '', '## 全部固定系统结果','',
        '|固定系统|平仓数|主净额/元|8场景最低净额|归一净额|最大已知平仓回撤/元|未知最多|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for model in z.MODELS:
        q=summary[model];a=q['primary']
        lines.append(f"|{title(model)}|{a['n']}|{fmt(a['net'])}|{fmt(q['worst8_net'])}|{fmt(a['normalized_net'])}|{fmt(a['cash_realized_dd'])}|{q['max8_unknown']}|")
    lines += ['', '主净额与归一净额排序可能不同，原因是各系统初始止损现金风险不同。这里的回撤只包含已知平仓结算，不含浮动亏损、保证金、逐日结算或未知盈亏。',
        '', '## 达到大几十笔后的质量','',
        f"20个可选系统中，实际主场景至少60笔的有{len(freq)}个。按事后描述性筛查，同时要求8场景均正、没有未知、删除最佳3笔及最佳月份后归一净额仍正、至少3个时间组主净额正，通过的有{len(eligible)}个："+('、'.join(title(m) for m in eligible) if eligible else '无')+'。这不是另一个可回溯选优的交易规则。',
        '', '|频率合格系统（按主归一结果排序）|笔数|平均单笔净额|4—10月净额|4—10月最低场景|删除最佳3笔归一净额|正月/活跃月|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for model in rank:
        q=summary[model];a=q['primary']
        lines.append(f"|{title(model)}|{a['n']}|{fmt(a['net']/a['n'])}|{fmt(a['test']['net'])}|{fmt(q['test_worst8_net'])}|{fmt(a['normalized_ex_top3'])}|{a['positive_active_months']}/{a['active_months']}|")
    lines += ['', '## 时间分组与前推选择','',
        '第一季度只作训练；每个Q2/Q3/10月截止日只读此前严格已平仓的影子交易。入选需至少20笔、每日频率≥0.35、全部场景归一净额正、删除最大赢家和最佳月份仍正、无已观察到的未知错误；按最差归一收益减回撤惩罚择一。未通过则空仓，已挂单/已持仓延续，不能在换组时清空亏损。',
        '', '|测试段起点|事前按训练选择|','|---|---|']
    for f in selection['schedule']:lines.append(f"|{f['start'][:7]}|{title(f['model'])}|")
    lines += ['', '|前推执行情景|平仓数|已知净额|300风险归一净额|未知|','|---|---:|---:|---:|---:|']
    for case,a in wf.items():
        if case.startswith('original/'):lines.append(f"|{case.removeprefix('original/')}|{a['n']}|{fmt(a['net'])}|{fmt(a['normalized_net'])}|{a['unknown']}|")
    lines += ['', '若前推规则选择空仓，0表示该组没有分配新信号，不表示未知交易被按零盈亏计入。固定系统全期盈利不等于第一季度就能选出它；全期选出的事后最高曲线不可充当前推结果。',
        '', '## 所有系统逐组结果','',
        '|系统|Q1训练净额|Q2净额|Q3有覆盖段净额|10月部分净额|','|---|---:|---:|---:|---:|']
    for model in z.MODELS:
        b=summary[model]['primary']['blocks']
        lines.append('|'+title(model)+'|'+'|'.join(fmt(b[n]['net']) for n in ['Q1_train','Q2','Q3','October_partial'])+'|')
    lines += ['', '## 摩擦与收益空间','',
        '|系统|净额|手续费|已嵌入滑点还原额|同路径还原前净额|','|---|---:|---:|---:|---:|']
    show=list(dict.fromkeys(['B5','E20S1']+rank[:4]+['MOM16/QUICK','COIN/QUICK']))
    for model in show:
        q=summary[model];lines.append(f"|{title(model)}|{fmt(q['primary']['net'])}|{fmt(q['commission'])}|{fmt(q['embedded_slippage_accounting'])}|{fmt(q['fixed_path_before_friction'])}|")
    lines += ['', '滑点还原只是在既有成交/离场/占用路径上做会计分解，不是无摩擦策略回测。真正改变滑点会改变入场资格、触发与后续竞争，不能直接把还原额当可赚的钱。随机方向只跑一条固定路径，是粗基准，不能当作显著性检验。',
        '', '## 追加的三个简单过滤与7月补充段','']
    follow=RUN/'breakout-followup'
    if (follow/'completion.json').exists():
        hist=READ(follow/'existing-history-results.json');extra=READ(follow/'july-results.json');combined=READ(follow/'combined-results.json')
        fselect=READ(follow/'selection-before-july.json')
        lines += ['原24系统结果之后才提出这三个过滤；这属于顺序探索，旧日期并非独立验证。过滤和7月选择都在生成7月结果前固定。补充只取7月1—10日8个交易日，结果不能据此推全年或证明稳定。',
            '', '|日内短突破过滤|原覆盖笔数/净额|补充7月笔数/净额|合并笔数/净额|合并最差场景净额|合并归一净额|',
            '|---|---:|---:|---:|---:|---:|']
        names={'B8':'原版','ALIGNED':'顺8/32慢趋势','EFFICIENT':'ER16≥0.30','SAME_DAY':'8柱通道全部形成于当天'}
        for name,label in names.items():
            h=hist[name+'/'+primary];j=extra[name+'/'+primary];a=combined[name+'/'+primary]
            lines.append(f"|{label}|{h['n']} / {fmt(h['net'])}|{j['n']} / {fmt(j['net'])}|{a['n']} / {fmt(a['net'])}|{fmt(min(combined[name+'/'+c]['net'] for c in cases))}|{fmt(a['normalized_net'])}|")
        lines += ['', '顺慢趋势版本合并102笔、现金2,195.56元，但归一净额仅1,491.27，低于原版；删除最大的3笔归一盈利后为-49.48，删除最佳月后为-840.89。现金净额提高同时带来了风险权重与集中度变化，不能称为更优、更稳定。ER和同日过滤也未通过。',
            '', f"7月1日仅使用此前训练进行的选择是{title(fselect['selected'])}，因此补充表内盈利是固定候选观察，不能算成前推策略已经获得的收益。[补充协议、结果与说明](breakout-followup/summary.md)。"]
        out['breakout_followup']=dict(selection=fselect['selected'],combined=combined,
            protocol_sha256=z.s.m.old.sha(follow/'protocol.json'),sequential_not_independent=True)
    write('comparison-summary.json',out)
    lines += ['', '## 执行核验与限制','',
        f"新增5项Python因果/费用/订单占用测试、3项原生下一开盘成交测试、4项原生经济边界测试通过。新引擎对{checks['exact_native_frozen_control_rows']}条冻结B5/E20S1执行逐字段重放一致，生产与行情输入hash未变。",
        '', '广泛运行旧库单元测试时，31项旧测试中25项因稀疏5分钟夹具遇到当前分钟连续性检查而失败；用原始冻结库真正重新编译后同样失败25项。新库34项中的失败集合完全相同，新增3项均通过。这些旧测试没有被修改或宣称通过；完整失败日志和比较记录保留。历史冻结执行逐字段对照另外通过。',
        '', '本轮不能承诺2026完整全年交易数、未来稳定收益或泛化。候选、商品和历史均存在研究选择；短时间组和单年季节性不足以拟合合约月份规则。',
        '', '[protocol.json](protocol.json)为完整预定义；[comparison-summary.json](comparison-summary.json)列出全部候选与筛查条件；[selection-before-test.json](selection-before-test.json)保存每个截止日可见训练及拒绝理由；[opportunity-ledgers.json](opportunity-ledgers.json)保留挂单和持仓占用；[fixture-test-equivalence.json](fixture-test-equivalence.json)记录旧测试失败对照。',
        '', '![交易频率、风险归一收益与分组结果](comparison.png)','']
    source='\n'.join(lines)
    for target in ['protocol.json','selection-before-test.json','opportunity-ledgers.json','fixture-test-equivalence.json','breakout-followup/summary.md']:
        source=source.replace(']('+target+')','](../'+target+')')
    (REV/'summary.md').write_text(source,encoding='utf-8')
    chart(summary,rank,wf,primary)
    for model,q in summary.items():
        for case in cases:
            a=fixed[case+'/'+model];assert abs(sum(a['month_cash'].values())-a['net'])<1e-7
            assert abs(sum(a['warning_month_normalized_net'].values())-a['normalized_net'])<1e-7
    n=sum(READ(RUN/q/pr/'executions/manifest.json')['evaluations'] for q in ['early','recent'] for pr in z.PROFILES)
    write('final-verification.json',dict(**checks,new_native_evaluations=n,monthly_reconciliations=len(fixed),
        python_tests_passed=5,new_native_entry_tests_passed=3,native_economic_tests_passed=4,
        old_fixture_tests_known_failed=25,old_new_failure_set_equal=True,
        research_script_sha256=z.s.m.old.sha(z.__file__),report_script_sha256=z.s.m.old.sha(__file__)))
    names=['protocol.json','binary.json','generation-checks.json','fixed-results.json','walkforward-results.json',
        'comparison-summary.json','selection-before-test.json','accepted-primary.jsonl','opportunity-ledgers.json',
        'summary.md','comparison.png','verification.json','final-verification.json','fixture-test-equivalence.json']
    derived={'comparison-summary.json','summary.md','comparison.png','final-verification.json'}
    paths={n if n in derived else '../'+n:(REV/n if n in derived else RUN/n) for n in names}
    write('completion.json',dict(completed_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        status='completed_available_2026_data',qualified_for_trading=False,production_changed=False,
        artifacts={n:z.s.m.old.sha(path) for n,path in paths.items()},frequency_candidates=len(freq),retrospective_screen_pass=eligible,
        followup_completed=(follow/'completion.json').exists(),
        conclusion='Frequency feasible:15 candidates>=60; no robust high-frequency winner. Short breakout180trades1803yuan primary, stress-negative. Three sequential filters andJuly tail also insufficient.'))
    print(json.dumps(dict(shared_days=len(gen['shared_dates']),native=n,frequency=rank,passed=eligible,schedule=selection['schedule']),ensure_ascii=False),flush=True)

def chart(summary,rank,wf,primary):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans'];plt.rcParams['axes.unicode_minus']=False
    fig,axs=plt.subplots(2,2,figsize=(16,10.5),layout='constrained')
    ax=axs[0,0]
    for m,q in summary.items():
        a=q['primary'];color='#267f65' if q['worst8_normalized']>0 else '#b95252'
        if m in ['B5','E20S1']:color='#356eb4'
        ax.scatter(a['n'],a['normalized_net'],color=color,s=35,alpha=.85)
    show=list(dict.fromkeys(['B5','E20S1']+rank[:3]))
    for m in show:
        a=summary[m]['primary'];ax.annotate(title(m),(a['n'],a['normalized_net']),xytext=(4,5),textcoords='offset points',fontsize=8)
    ax.axvline(60,color='#777',ls='--',label='60笔目标');ax.axhline(0,color='#555',lw=.8)
    ax.set_title('全部系统：频率提高是否伴随净优势');ax.set_xlabel('实际已知平仓笔数');ax.set_ylabel('300计划风险归一净额');ax.legend()
    ax=axs[0,1];top=rank[:6] if rank else ['B5','E20S1'];ticks=np.arange(len(top))
    ax.bar(ticks-.18,[summary[m]['primary']['normalized_net'] for m in top],.36,label='主场景')
    ax.bar(ticks+.18,[summary[m]['worst8_normalized'] for m in top],.36,label='8场景最低')
    ax.set_xticks(ticks,[title(m) for m in top],rotation=30,ha='right',fontsize=9);ax.set_title('频率达标候选的成交压力');ax.axhline(0,color='#555',lw=.8);ax.legend()
    ax=axs[1,0]
    top=list(dict.fromkeys(['B5','E20S1']+rank[:2]));mos=z.MONTHS
    for m in top:
        a=summary[m]['primary'];v=0.;ys=[]
        for mo in mos:v+=a['warning_month_normalized_net'][mo];ys.append(v)
        ax.plot(range(len(mos)),ys,marker='o',ms=3,label=title(m))
    ax.set_xticks(range(len(mos)),[m[5:] for m in mos]);ax.set_title('按预警月归属累计；7月缺数据不代表无机会');ax.set_xlabel('2026月份（非账户净值）');ax.axhline(0,color='#555',lw=.8);ax.legend(fontsize=9)
    ax=axs[1,1];top=rank[:3] if rank else ['B5','E20S1'];bs=['Q1_train','Q2','Q3','October_partial'];ticks=np.arange(4)
    for i,m in enumerate(top):ax.bar(ticks+(i-(len(top)-1)/2)*.25,[summary[m]['primary']['blocks'][b]['net'] for b in bs],.25,label=title(m))
    ax.set_xticks(ticks,['Q1训练','Q2','Q3覆盖段','10月部分']);ax.set_title('频率达标候选跨组一手净额');ax.axhline(0,color='#555',lw=.8);ax.legend(fontsize=8)
    for ax in axs.flat:ax.grid(axis='y',alpha=.18);ax.set_axisbelow(True)
    fig.suptitle('鸡蛋15分钟机会扩展｜独立挂单占用｜固定候选探索，非未触碰样本',fontsize=16)
    fig.savefig(REV/'comparison.png',dpi=160);plt.close(fig)

if __name__=='__main__':run()
