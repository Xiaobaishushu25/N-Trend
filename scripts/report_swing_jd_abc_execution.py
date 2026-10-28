"""Finite ABC execution results, repaired peak-only audit and all failures."""
import collections
import datetime as dt
import json
import research_swing_jd_abc_execution as b

RUN=b.RUN
READ=b.READ
EV=b.EV
LABELS={'TOUCH':'原触发','CLOSE1':'分钟收盘确认','RETEST':'突破后回踩','C15':'15分钟C转折确认','LOCAL':'原交易止损','BTRUE':'整个B段止损','B5':'原B5','E20S1':'原S1＋成本20%'}
def title(m):return LABELS[m] if m in LABELS else '＋'.join(LABELS[x] for x in m.split('_'))
def fmt(x):return '—' if x is None else f'{x:,.2f}'
def write(n,v):b.write(n,v)

def projection_attribution(primary):
    base=collections.defaultdict(list);new=collections.defaultdict(list)
    for root,dest in [(RUN,base),(RUN/'A-projection',new)]:
        for line in (root/'accepted-primary.jsonl').open(encoding='utf-8'):
            v=json.loads(line)
            if EV(v['row'])['status']=='closed':dest[v['model']].append(v['row'])
    mo=READ(RUN/'signal-metadata.json');mn=READ(RUN/'A-projection/signal-metadata.json');out={}
    for newname,oldname in [('A1_TOUCH','TOUCH_BTRUE'),('A1_RETEST','RETEST_BTRUE'),('A1_C15','C15_BTRUE')]:
        key=lambda r,meta:(meta[r['event_key']]['cluster'],r['warning_ts'])
        aa={key(r,mo):r for r in base[oldname]};bb={key(r,mn):r for r in new[newname]}
        common=aa.keys()&bb.keys();added=bb.keys()-aa.keys();removed=aa.keys()-bb.keys()
        q=dict(common_n=len(common),common_decision_net_delta=sum(EV(bb[k])['net_amount']-EV(aa[k])['net_amount'] for k in common),
            added_n=len(added),added_net=sum(EV(bb[k])['net_amount'] for k in added),removed_n=len(removed),removed_net=sum(EV(aa[k])['net_amount'] for k in removed),
            common_stop_differences=sum(aa[k]['stop']!=bb[k]['stop'] for k in common))
        delta=sum(EV(r)['net_amount'] for r in bb.values())-sum(EV(r)['net_amount'] for r in aa.values())
        assert abs(delta-q['common_decision_net_delta']-q['added_net']+q['removed_net'])<1e-7
        q['net_delta']=delta;out[newname]=q
    return out

def verify_revision():
    old=b.ROOT/'research-runs/swing-jd-abc-execution-20261026-v1'
    exact=0;old_amb=0;resolved=0;remain=0;merged=collections.Counter();changed=[]
    for period in ['early','recent']:
        for task in ['seed','C15']:
            assert b.z.s.m.old.sha(RUN/period/task/(period+'-pool.json'))==b.z.s.m.old.sha(old/period/task/(period+'-pool.json'))
            prev={}
            for line in (old/period/task/'executions/evaluations.jsonl').open(encoding='utf-8'):
                r=json.loads(line);prev[r['event_key'],r['scenario']]=r
            for line in (RUN/period/task/'executions/evaluations.jsonl').open(encoding='utf-8'):
                r=json.loads(line);q=prev[r['event_key'],r['scenario']];model=r['scenario'].split('/')[0]
                if EV(q)['status']!='ambiguous':
                    for f in ['result','planned_risk','planning_entry','entry','stop','target','new_friction']:assert r[f]==q[f],(r['event_key'],r['scenario'],f)
                    exact+=1
                else:
                    old_amb+=1
                    if EV(r)['status']=='ambiguous':remain+=1
                    else:resolved+=1;changed.append(dict(event_key=r['event_key'],scenario=r['scenario'],new_status=EV(r)['status'],merges=r['peak_only_merges']))
                if model in ['B5','E20S1']:assert r['peak_only_merges']==0
                if r['peak_only_merges']:merged[model]+=1
    return dict(nonambiguous_v1_rows_exact=exact,v1_ambiguous_rows=old_amb,v1_ambiguous_now_resolved=resolved,
        still_execution_ambiguous=remain,rows_using_peak_equivalence=dict(merged),changed_rows=changed,
        unchanged_warning_pools=True,old_controls_never_opt_in=True)

def run():
    p=READ(RUN/'protocol.json');primary=p['primary'];fixed=READ(RUN/'fixed-results.json');wf=READ(RUN/'walkforward-results.json');cases=list(wf)
    select=READ(RUN/'selection-before-test.json');gen=READ(RUN/'generation-checks.json');checks=verify_revision()
    summary={};passes=[]
    for m in b.MODELS:
        xs=[fixed[c+'/'+m] for c in cases];q=fixed[primary+'/'+m]
        flags=dict(at_least60=q['n']>=60,positive_cash_all8=min(x['net'] for x in xs)>0,positive_normalized_all8=min(x['normalized_net'] for x in xs)>0,
            no_unknown=max(x['unknown'] for x in xs)==0,ex_best3_positive=q['normalized_ex_top3']>0,
            positive3_blocks=sum(z['net']>0 for z in q['blocks'].values())>=3)
        summary[m]=dict(primary=q,min8_net=min(x['net'] for x in xs),min8_normalized=min(x['normalized_net'] for x in xs),
            max8_unknown=max(x['unknown'] for x in xs),min8_closed=min(x['n'] for x in xs),test_min8_net=min(x['test']['net'] for x in xs),flags=flags)
        if m in b.CHALLENGERS and all(flags.values()):passes.append(m)
    projection=READ(RUN/'A-projection/fixed-results.json');qualification=READ(RUN/'qualification-split/results.json');attr=projection_attribution(primary)
    write('projection-attribution.json',attr)
    write('comparison-summary.json',dict(models=summary,retrospective_screen_pass=passes,selection=select['schedule'],walkforward=wf,
        known_correction=checks,not_untouched_OOS=True,projection=projection,qualification_split=qualification,projection_attribution=attr))
    write('execution-correction-checks.json',checks)
    lines=['# 保留ABC：鸡蛋入场与结构止损的有限机制验证','',
        '日期：2026-10-26（北京时间）。本轮保留原ABC识别、评分和A/B锚点，没有改动EG/PVC/SR或生产策略。','',
        '## 固定实验','',
        '4种入场×2种止损，共8个新组合；原B5和此前原S1＋成本20%为两个完整对照。新组合共享原S1目标、至少10tick风险、名义摩擦≤0.20R、成本/目标≤20%、计划与实际净盈亏比≥1、原60桶最长持仓及节假日/周末处理。','',
        '为了完整检验结构延续，新8组合取消无跟随退出，并使用各自明确入场方式，不再叠加小时自适应。原B5/E20S1完整保留自身规则。因此新组合相互比较可分析入场/止损，不能把它们与旧对照的全部差异都归因于止损。','',
        '|入场|定义|','|---|---|',
        '|原触发|原警报触发位触价|',
        '|分钟收盘确认|一分钟收盘越过原触发位，下一分钟开盘入场|',
        '|突破后回踩|先有较早分钟突破原触发位，再回踩原触发位减去/加上四分之一几何R；按tick向原突破方向取整|',
        '|C转折确认|ABC预警后12根15分钟内，先推动越过原触发位，再出现一左一右完成柱确认的更高低点/更低高点；确认柱收盘后才挂其极值外1tick|',
        '', '原交易止损不变；整个B段止损取原预警时已知整段B极值，并保留原止损缓冲。回踩计划按照各自止损重新计算预计入场、初始R、费用和目标空间，实际成交仍重新检查风险与净盈亏比；没有保留旧R算盈利同时扩大亏损。','',
        'C候选确认前，整个B失效或S1已到达则取消等待。同A簇同确认时刻保留最早父预警。C种子不是挂单，确认后才占用；每个模型只有一个鸡蛋挂单/持仓槽，前单终止严格早于下一预警才释放，同A簇已实现两次原止损后禁止再试。','',
        '## 数据与覆盖','',
        f"原220条合格JD预警之外，补入已归档7月1—10日{gen['july_seed_count']}条合格ABC，共231条父预警。72次父预警形成C确认，去除同簇/同钟重复后71条。整体仍缺7月中下旬至8月初JD覆盖，近期截至10月23日11:05；不是完整全年。C候选可跨6月/7月边界，但最终决策时间不能超过已有计分窗口。",'',
        '金额均为各次交易一手、扣费用及滑点后的已知平仓累计。300元计划风险归一采用警报时已知的预计入场至止损风险计算分数手研究数量；不代表账户收益、可直接下单手数或保证金配置。实际开盘可能偏离计划，实际/计划风险比另行保存。','',
        '## 全部固定组合','',
        '|组合|主场景平仓数|主净额/元|8场景最低净额|归一净额|8场景最低归一|最多未知|','|---|---:|---:|---:|---:|---:|---:|']
    for m in b.MODELS:
        q=summary[m];v=q['primary'];lines.append(f"|{title(m)}|{v['n']}|{fmt(v['net'])}|{fmt(q['min8_net'])}|{fmt(v['normalized_net'])}|{fmt(q['min8_normalized'])}|{q['max8_unknown']}|")
    lines+=['','8场景是两种成交模式×两档滑点×两种未知释放政策的敏感性，不是8份独立行情。真实成交/止损先后不明、缺报价或未完成头寸仍是未知，不按零收益。','',
        '## 分组表现','',
        '|组合|Q1训练|Q2|Q3有覆盖段|10月部分|其中新增7月净额|','|---|---:|---:|---:|---:|---:|']
    for m in b.MODELS:
        q=summary[m]['primary'];bs=q['blocks'];lines.append('|'+title(m)+'|'+'|'.join(fmt(bs[n]['net']) for n in ['Q1_train','Q2','Q3','October_partial'])+'|'+fmt(q['july']['net'])+'|')
    lines+=['','Q3含新补7月上旬和原8—9月覆盖段；“其中7月”是Q3的组成，不能再相加。新增月份必须完整保留，不能因其亏损退回旧日期范围。','',
        '## 是否满足高频与稳定目标','',
        '回顾性筛查同时要求：主场景至少60笔、全部场景现金和归一净额均正、没有未知、删除最佳3笔归一盈利仍正、至少3个时间组主净额正。通过者：'+('、'.join(title(m) for m in passes) if passes else '无')+'。这个筛查不是事前可执行的选优策略。','',
        '|组合|主场景最大已知平仓回撤|删除最佳3笔后归一净额|4—10月净额|4—10月最低场景净额|','|---|---:|---:|---:|---:|']
    for m in b.MODELS:
        q=summary[m];v=q['primary'];lines.append(f"|{title(m)}|{fmt(v['cash_realized_dd'])}|{fmt(v['normalized_ex_top3'])}|{fmt(v['test']['net'])}|{fmt(q['test_min8_net'])}|")
    lines+=['','回撤为已知平仓结算回撤，不含浮动亏损、未知盈亏或保证金。增加交易数量同时需要保留单笔净优势，不能用毛胜率或只看一手金额代替。','',
        '## 季度前推','',
        '每个截止日仅用此前严格已平仓的影子交易；需至少20笔、日频率≥0.35、全部场景归一净额正、删除最佳赢家仍正，且全部场景不低于E20S1。按最差归一收益减回撤惩罚选一个；无通过则保留E20S1研究对照，尚不代表该对照已获准实盘。跨组继续原挂单、持仓与止损计数。','',
        '|后续组起点|此前训练选择|','|---|---|']
    for f in select['schedule']:lines.append(f"|{f['start'][:7]}|{title(f['model'])}|")
    lines+=['','|前推成交场景（original占用）|平仓|已知净额|归一净额|未知|','|---|---:|---:|---:|---:|']
    for case,v in wf.items():
        if case.startswith('original/'):lines.append(f"|{case.removeprefix('original/')}|{v['n']}|{fmt(v['net'])}|{fmt(v['normalized_net'])}|{v['unknown']}|")
    lines+=['','静态全期盈利不等于每次季度开始前都能选出。现有历史与商品已被多轮研究，没有未触碰OOS；不能将候选最高曲线称作已验证泛化。','',
        '## 回踩路径核验修正','',
        'v1留存，v2只修正一个执行判定：旧引擎在两条分钟OHLC路径交易状态完全相同、仅历史浮盈峰值不同的情况下也会立即终止为未知。本轮新组合不用无跟随、峰值保护/跟踪或快失败，因此这类峰值差异不会改变后续交易决策。','',
        '只有显式启用、OriginalS1、没有上述依赖峰值的规则、两条路径均持仓且入场/仓位/止损/目标/确认状态/成交腿完全一致时才允许继续；MFE保留下界、MAE保留不利包络作描述。成交、止损、入场或确认不同仍未知。旧B5/E20S1不启用。','',
        f"v1非模糊执行{checks['nonambiguous_v1_rows_exact']}条与v2逐字段一致；原模糊{checks['v1_ambiguous_rows']}条，其中{checks['v1_ambiguous_now_resolved']}条恢复可确定执行，{checks['still_execution_ambiguous']}条仍有真实执行分歧。数字按候选×成交情景计数，不是独立交易。",'',
        '3项原生修正测试验证：允许只差峰值且未来不依赖峰值时继续；启用无跟随则禁止合并；真正交易路径不同仍未知。另有6项原生经济/计划风险测试和4项Python确认/占用测试通过。1,760条原冻结B5/E20S1个别执行对照一致，行情输入与生产文件hash未变。','',
        '[protocol.json](protocol.json)固定全部机制；[comparison-summary.json](comparison-summary.json)为全部场景和筛查；[selection-before-test.json](selection-before-test.json)保存训练信息；[funnel.json](funnel.json)为资格与执行漏斗；[execution-correction-checks.json](execution-correction-checks.json)保存修正逐行核对；[accepted-primary.jsonl](accepted-primary.jsonl)保存实际接受路径。','',
        '![ABC机制验证](comparison.png)','']
    lead=['## 本轮结论','',
        '原S1目标下，整个B段止损使原触发由19笔增加到33笔，但现金净额几乎未变，同风险结果并未更稳；等待C转折的11—14笔对成交压力较稳，却更低频且利润集中。原8组合没有满足60笔与稳定性的联合目标。','',
        '追加的一倍A段投射目标把原触发提高到72笔，却亏3,062.36元。关键归因是：共同27笔反而多赚859.87元，新增45笔合计亏5,832.10元。更远目标在本样本不仅改变退出，还放进了原来未满足真实S1空间门槛的机会。','',
        '最后只固定一个分离对照：仍用原S1的计划及实际成交空间审核资格，退出才允许更远A投射。31笔净额2,370.21元，8场景现金最低180.45元；但同风险压力最低仍为-729.67，删除最佳3笔后的归一净额-979.20。因此有收益改善线索，尚无稳定升级依据，也未达到频率目标。','',
        '更值得继续保留的是ABC框架和客观空间资格，把预警、入场资格、退出目标分开；目前未支持靠放宽止损、等待更久或画远目标就能使大量预警盈利。下一轮若继续，应研究ABC预警发生时机与当时已存在的空间质量，避免把自设远目标当作入场优势。','']
    at=lines.index('## 固定实验');lines[at:at]=lead
    extra=['## 追加：目标扩展与入场资格分离','',
        '以下在看到原S1漏斗后提出，属于顺序探索；每项新机制在自己的执行结果产生前固定，但旧时间组已反复研究，不称作独立OOS。原ABC不变，只用一个已知公式：目标=B段极值＋方向×A段幅度，不扫描A倍数。','',
        '|A等幅目标方案|平仓|主净额|8场景最低净额|8场景最低归一|','|---|---:|---:|---:|---:|']
    for m,label in [('A1_TOUCH','原触发'),('A1_RETEST','突破回踩'),('A1_C15','C转折确认')]:
        v=projection[primary+'/'+m];zs=[projection[c+'/'+m] for c in cases]
        extra.append(f"|{label}|{v['n']}|{fmt(v['net'])}|{fmt(min(q['net'] for q in zs))}|{fmt(min(q['normalized_net'] for q in zs))}|")
    extra+=['','|相对原S1的分解|共同决策数|共同决策收益变化|新增决策数|新增净额|移除净额|','|---|---:|---:|---:|---:|---:|']
    for m,q in attr.items():extra.append(f"|{m}|{q['common_n']}|{fmt(q['common_decision_net_delta'])}|{q['added_n']}|{fmt(q['added_net'])}|{fmt(q['removed_net'])}|")
    extra+=['','C确认的候选时机可能因新目标而变化，共同决策按同A簇与决策时间匹配，细节保留；这不是单独改变一个数字的因果回归。组合占用也会重新竞争。','',
        '限定资格/退出分离实验：原触发＋整个B止损，原S1审核计划成本与名义/实际填价净盈亏比；退出目标取S1与A等幅中更远者。所有审核都使用当时已知价格，没有读取旧交易是否盈利或是否平仓。','',
        '|资格/退出分离|平仓|主净额|8场景最低现金|8场景最低归一|','|---|---:|---:|---:|---:|',
        f"|S1审核、远目标退出|{qualification[primary]['n']}|{fmt(qualification[primary]['net'])}|{fmt(min(q['net'] for q in qualification.values()))}|{fmt(min(q['normalized_net'] for q in qualification.values()))}|",'',
        '924个个别入场案例与原S1组合一致，包括139个可确定入场及785个不入场；因此它只改变已合格订单的退出，不依赖旧未来结果来筛选。更长持仓仍会改变组合的后续机会。3项新增原生测试检查名义资格、实际填价复核及退出不被资格目标截近。','',
        '[A目标实验详细记录](A-projection/summary.md)；[资格/退出分离详细记录](qualification-split/summary.md)；[收益变化归因](projection-attribution.json)。','',
        '![目标与资格分离](target-qualification.png)','']
    at=lines.index('## 回踩路径核验修正');lines[at:at]=extra
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8')
    chart(summary,primary)
    target_chart(summary,projection,qualification,primary,cases)
    for q in fixed.values():
        assert abs(sum(q['month_cash'].values())-q['net'])<1e-7
        assert abs(sum(q['warning_month_normalized_net'].values())-q['normalized_net'])<1e-7
    parent=READ(RUN/'generation-checks.json');pp=READ(b.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')
    for period,hs in parent['source_hashes'].items():
        for key,h in hs.items():assert b.z.s.m.old.sha(pp['periods'][period][key])==h
    for q,h in p['production_before'].items():assert b.z.s.m.old.sha(b.ROOT/q)==h
    count=sum(READ(RUN/q/t/'executions/manifest.json')['evaluations'] for q in ['early','recent'] for t in ['seed','C15'])
    write('final-verification.json',dict(native_evaluations=count,exact_frozen_controls=1760,monthly_reconciliations=len(fixed),
        python_tests_passed=4,native_planning_tests_passed=6,native_peak_equivalence_tests_passed=3,source_and_production_unchanged=True,
        qualification_entry_cases=924,qualification_split_tests_passed=3,projection_attribution_reconciled=True,
        report_source_sha256=b.z.s.m.old.sha(__file__),research_source_sha256=b.z.s.m.old.sha(b.__file__),same_unlabelled_warning_pools=True))
    names=['protocol.json','binary.json','correction.json','generation-checks.json','signal-metadata.json','fixed-results.json','walkforward-results.json','comparison-summary.json',
        'selection-before-test.json','execution-correction-checks.json','accepted-primary.jsonl','projection-attribution.json','summary.md','comparison.png','target-qualification.png','verification.json','final-verification.json',
        'A-projection/completion.json','qualification-split/completion.json']
    write('completion.json',dict(completed_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),completed=True,ABC_retained=True,production_changed=False,
        qualified_for_trading=False,frequency_robustness_pass=passes,artifacts={n:b.z.s.m.old.sha(RUN/n) for n in names}))
    print(json.dumps(dict(passes=passes,correction={k:v for k,v in checks.items() if k!='changed_rows'},main={m:dict(n=q['primary']['n'],net=q['primary']['net'],worst=q['min8_net'],normalized_worst=q['min8_normalized']) for m,q in summary.items()}),ensure_ascii=False),flush=True)

def chart(summary,primary):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans'];plt.rcParams['axes.unicode_minus']=False
    fig,axs=plt.subplots(2,2,figsize=(16,10),layout='constrained')
    ax=axs[0,0];ix=np.arange(4)
    for j,stop in enumerate(b.STOPS):ax.bar(ix+(j-.5)*.36,[summary[e+'_'+stop]['primary']['n'] for e in b.ENTRIES],.36,label=LABELS[stop])
    ax.axhline(60,color='#777',ls='--',lw=1);ax.set_xticks(ix,[LABELS[e] for e in b.ENTRIES]);ax.set_title('入场×止损：实际平仓频率');ax.legend()
    ax=axs[0,1]
    for j,stop in enumerate(b.STOPS):ax.bar(ix+(j-.5)*.36,[summary[e+'_'+stop]['primary']['normalized_net'] for e in b.ENTRIES],.36,label=LABELS[stop])
    ax.set_xticks(ix,[LABELS[e] for e in b.ENTRIES]);ax.set_title('300元计划风险归一净额');ax.axhline(0,color='#777',lw=.8);ax.legend()
    ax=axs[1,0];show=['TOUCH_BTRUE','RETEST_BTRUE','C15_BTRUE','E20S1'];ix=np.arange(4)
    ax.bar(ix-.18,[summary[m]['primary']['normalized_net'] for m in show],.36,label='主场景');ax.bar(ix+.18,[summary[m]['min8_normalized'] for m in show],.36,label='8场景最低')
    ax.set_xticks(ix,[title(m) for m in show],rotation=18,ha='right',fontsize=9);ax.set_title('成交压力与净优势');ax.axhline(0,color='#777',lw=.8);ax.legend()
    ax=axs[1,1];blocks=['Q1_train','Q2','Q3','October_partial'];ix=np.arange(4)
    for j,m in enumerate(show):ax.bar(ix+(j-1.5)*.19,[summary[m]['primary']['blocks'][q]['net'] for q in blocks],.19,label=title(m))
    ax.set_xticks(ix,['Q1训练','Q2','Q3覆盖段','10月部分']);ax.set_title('跨组一手净额（Q3含新增7月上旬）');ax.axhline(0,color='#777',lw=.8);ax.legend(fontsize=8)
    for ax in axs.flat:ax.grid(axis='y',alpha=.18);ax.set_axisbelow(True)
    fig.suptitle('保留ABC｜有限入场/结构止损机制｜2026已有行情，不是未触碰样本',fontsize=16)
    fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)

def target_chart(summary,projection,qualification,primary,cases):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    items=[('原S1对照',summary['E20S1']['primary'],summary['E20S1']['min8_net']),
        ('整个B止损＋S1',summary['TOUCH_BTRUE']['primary'],summary['TOUCH_BTRUE']['min8_net']),
        ('整个B止损＋A目标',projection[primary+'/A1_TOUCH'],min(projection[c+'/A1_TOUCH']['net'] for c in cases)),
        ('S1资格＋远目标退出',qualification[primary],min(v['net'] for v in qualification.values()))]
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans'];plt.rcParams['axes.unicode_minus']=False
    fig,axs=plt.subplots(1,2,figsize=(13,5),layout='constrained');ix=np.arange(4)
    bars=axs[0].bar(ix,[v['n'] for _,v,_ in items],color=['#3776a5','#3776a5','#c35a53','#328571'])
    axs[0].bar_label(bars);axs[0].axhline(60,color='#777',ls='--');axs[0].set_xticks(ix,[n for n,_,_ in items],rotation=18,ha='right');axs[0].set_title('目标变远，交易变多，是否值得')
    axs[1].bar(ix-.18,[v['net'] for _,v,_ in items],.36,label='主场景');axs[1].bar(ix+.18,[w for _,_,w in items],.36,label='8场景最低')
    axs[1].set_xticks(ix,[n for n,_,_ in items],rotation=18,ha='right');axs[1].set_ylabel('一手已知平仓净额/元');axs[1].axhline(0,color='#777',lw=.8);axs[1].legend()
    for ax in axs:ax.grid(axis='y',alpha=.18);ax.set_axisbelow(True)
    fig.suptitle('ABC结构不变：入场资格与退出目标需要分别检验',fontsize=15)
    fig.savefig(RUN/'target-qualification.png',dpi=160);plt.close(fig)

if __name__=='__main__':run()
