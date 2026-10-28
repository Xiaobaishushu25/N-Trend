"""Consolidate frozen cycle studies, keeping unsuccessful rules and uncertainty."""
import collections,datetime as dt,json,math,pathlib,random,statistics
import research_swing_cycle as c
ROOT=c.RUN
RUNS=[ROOT,ROOT/'monetization-supplement',ROOT/'context-supplement',ROOT/'context-combinations']
CANDIDATE='P2_cycle_h60_kind_confirm'

def weekly_interval(rows,period):
    start,end=('2026-01-01','2026-06-30') if period=='early' else ('2026-07-01','2026-10-23')
    def week(s):
        d=dt.date.fromisoformat(s[:10]);return d-dt.timedelta(days=d.weekday())
    bins={};cursor=week(start)
    while cursor<=week(end):bins[cursor]=[];cursor+=dt.timedelta(days=7)
    for r in rows:
        if c.ev(r)['status']=='closed':bins[week(r['warning_ts'])].append(c.ev(r)['net_r'])
    weeks=list(bins.values());rng=random.Random(20261025);means=[]
    for _ in range(2000):
        sample=[x for _ in weeks for x in rng.choice(weeks)]
        if sample:means.append(statistics.mean(sample))
    means.sort();return dict(weeks=len(weeks),mean_r95=[means[int(.025*len(means))],means[min(len(means)-1,int(.975*len(means)))]],
        note='Descriptive warning-week resampling across products, unknown trades excluded; no multiple-search correction or independent-validation claim.')

def run():
    results={r.name:c.read(r/'results.json') for r in RUNS};unique={}
    for r in RUNS:
        for name in c.read(r/'gates.json'):
            if name=='P2_cycle_h60_base':continue
            unique.setdefault(name,r.name)
    assert len(unique)==27,len(unique)
    events={x['event_key']:x['event'] for p in ('early','recent') for x in c.read(ROOT/(p+'-pool.json'))}
    baseline=collections.defaultdict(list);hour=collections.defaultdict(list);candidate=collections.defaultdict(list)
    for r,dest,name in [(ROOT,baseline,'P2_cycle_base'),(ROOT/'context-supplement',hour,'P2_cycle_h60_aligned'),(ROOT/'context-combinations',candidate,CANDIDATE)]:
        for line in (r/'accepted.jsonl').open(encoding='utf-8'):
            z=json.loads(line)
            if z['case'].split('/')[0]==name:dest[(z['period'],z['case'].split('/',1)[1])].append(z['row'])
    mechanism={};matched={};by_state={};uncertainty={};stress={};leave=c.read(ROOT/'context-combinations/leave-product-out.json')
    for period in ('early','recent'):
        a=[r for r in baseline[(period,'corrected_touch/slip1')] if c.ev(r)['status']=='closed'];fee=statistics.mean(r['cost_repricing']['commission']/(c.ev(r)['risk']*r['multiplier']) for r in a)
        slip=statistics.mean(2*r['slip']*r['tick_size']/c.ev(r)['risk'] for r in a);mean=statistics.mean(c.ev(r)['net_r'] for r in a)
        mechanism[period]=dict(n=len(a),mean_net_r=mean,fee_r=fee,nominal_slip_r=slip,same_ledger_friction_addback_r=mean+fee+slip,
            accounting_only='Adding nominal charges back to the same accepted fills is not a counterfactual zero-cost replay.',
            mfe1_reached=sum(c.ev(r)['mfe_r']>=1 for r in a),mfe1_then_loss=sum(c.ev(r)['mfe_r']>=1 and c.ev(r)['net_r']<0 for r in a))
        bins=collections.defaultdict(list)
        for r in a:
            h=(events[r['event_key']].get('market_context') or {}).get('trend_alignment_60')
            bins['missing' if h is None else 'aligned' if h>0 else 'counter'].append(r)
        by_state[period]={k:c.metrics(v) for k,v in bins.items()}
        aa={r['event_key']:r for r in hour[(period,'corrected_touch/slip1')] if c.ev(r)['status']=='closed'}
        bb={r['event_key']:r for r in a if (events[r['event_key']].get('market_context') or {}).get('trend_alignment_60',-1) is not None and (events[r['event_key']].get('market_context') or {}).get('trend_alignment_60',-1)>0}
        matched[period]=dict(aligned_baseline_n=len(bb),refiltered_n=len(aa),common=len(aa.keys()&bb.keys()),
            new_keys=sorted(aa.keys()-bb.keys()),removed_keys=sorted(bb.keys()-aa.keys()),
            new_net=sum(c.ev(aa[k])['net_amount'] for k in aa.keys()-bb.keys()),removed_net=sum(c.ev(bb[k])['net_amount'] for k in bb.keys()-aa.keys()))
        uncertainty[period]={};stress[period]={}
        for mode in ('corrected_touch','next_minute_open'):
            for slip in (1,2):
                suffix=f'{mode}/slip{slip}';case=CANDIDATE+'/'+suffix;m=results['context-combinations'][period][case];b=results[ROOT.name][period]['P2_cycle_base/'+suffix]
                uncertainty[period][suffix]=weekly_interval(candidate[(period,suffix)],period)
                l=[v[period] for v in leave[case].values()]
                stress[period][suffix]=dict(net=m['net'],mean_r=m['mean_r'],n=m['n'],win_rate=m['win_rate'],unknown=m['unknown'],
                    drawdown=m['realized_cash_drawdown'],tail10_cash=m['tail10_cash'],retention=m['net']/b['net'] if b['net']>0 else None,
                    leave_product_out_negative_r=sum(v['mean_r'] is not None and v['mean_r']<0 for v in l),leave_product_out_total=len(l),
                    leave_product_out_worst_r=min(v['mean_r'] for v in l if v['mean_r'] is not None),
                    without_best3products=m['without_best3products'])
    gate_sources={r.name:c.read(r/'gates.json') for r in RUNS}
    all_passed=[name for name,source in unique.items() if gate_sources[source][name]['passed']]
    observation=dict(id=CANDIDATE,status='research_observation_only',qualified_for_trading=False,production_changed=False,
        selected_after_inspecting_development_results=True,independent_holdout=False,unique_historical_templates=27,
        definition=dict(reference='P2_local_wide',trend='warning-time side*(closed60m EMA20-EMA60)/ATR20 > 0; missing rejects',
            entry='warning_kind cumulative: original touch; strong/wick: completed1m close confirms original entry, next1m open with adverse slippage',
            guard='No ATR band; score>=2.5,friction<=.2R,min10ticks,forecast and actual net reward/loss>=1,original chase/capacity/unknown/calendar guards.',
            exit='original S2 stop; nearest eligible pre-warning confirmed15m pivot target or S1; original no-follow5 and weekend/holiday rules. No new A breakout condition.'),
        result=stress,uncertainty=uncertainty,all_gates_passed=False,failures=['2tick early cash negative in both entry scenarios','recent cash retention fails80%','leave-product-out meanR can be negative','meanR intervals include zero','all periods repeatedly researched'],
        next_validation=dict(required_new_closed=200,required_new_trading_days=60,thresholds_from_existing_research='Quantity is a checkpoint, not automatic qualification.',
            baseline_shadow_required=True,record='Keep every warning, rejection, quote/trigger/order/fill timestamp, bid/ask spread, cost, actual risk and open/unknown exposure. Freeze this candidate and baseline before new records; no per-month/product tuning.',
            final_requirements='Positive cash AND averageR across new data and cost/entry stress; recent development retention deficiency remains unresolved; product/date concentration and tail risk must improve.',
            started=False),
        hashes={str(r/'protocol.json'):c.sha(r/'protocol.json') for r in RUNS})
    c.write(ROOT/'observation-candidate.json',observation)
    analysis=dict(mechanism=mechanism,baseline_same_cohort_hourly_state=by_state,capacity_effect_of_hourly_filter=matched,
        candidate=stress,weekly_uncertainty=uncertainty,unique_templates=unique,all_gates_passed=all_passed,
        no_live_changes=True,new_independent_data_tested=False)
    c.write(ROOT/'final-analysis.json',analysis)
    lines=['# 上半年负R：本轮结论','',
        '找到一个上半年平均净R在四种场景均为正的观察分支，但没有方案满足全周期稳定与近期收益保留80%的完整要求。没有修改实盘代码或参数。',
        '', '## 已做的工作','',
        '统一基线下共比较27个不同模板（含参考策略），覆盖入场时效、预警实体破坏、分钟确认、回踩站回、净空间、在线停开、目标兑现及60分钟/10日方向环境与组合。先冻结每批定义，再计算其结果；补充分支明确发生于前批结果之后。所有历史均为开发数据。',
        '', '## 真正有信息的新发现','',
        '在同一份基线已平仓账本内分组，排除重新竞争机会的影响：','',
        '|时期|60分钟方向环境|笔数|净额（元）|平均净R|','|---|---|---:|---:|---:|']
    for period,title in [('early','1—6月'),('recent','7—10月')]:
        for state,m in by_state[period].items():lines.append(f"|{title}|{state}|{m['n']}|{m['net']:.2f}|{m['mean_r']:.4f}|")
    lines+=['',
        '同向指预警方向与当时已完成60分钟EMA20/EMA60差同号，ATR20只作尺度归一。它描述N形处于何种方向环境，不要求A段突破此前高低点。分组是描述性关联，尚不能断言独立因果或未来稳定。同向组仍从上半年+0.0556R变到近期+0.2075R，反向组从−0.4612R变到+0.1260R，方向环境没有解释全部时期差异。',
        '', '上半年基线的反向组184笔净亏27164.67元，平均R−0.4612；同向组317笔净赚5895.20元，平均R+0.0556。重新筛选并计算机会竞争后，同向策略333笔+4941.28元/+0.0506R。两者的差额、释放及被替换机会详见final-analysis.json，不能把收益变化全归于过滤本身。',
        '', '近期60分钟特征缺失的23笔基线平仓赚4459.48元；拒绝缺失样本会减少近期收益。上下文审计覆盖7136个预警，近期原始池260个缺60分钟特征。不能用缺失样本事后盈亏决定是否放行；后续应先补齐因果预热数据，再重新冻结验证。',
        '', '数据覆盖审计发现：“7—10月”只是预警窗口名，各品种近期输入起点不齐。例如BU始于2026-08-25 21:55，EB/SH始于2026-09-16 13:55，多数品种始于7月14日。上半年输入从2025-12-01开始、延伸到2026-07-15。不能将这两组称为所有品种完整覆盖的十个月；目前没有重新补齐缺失历史，不能宣称已做完整全周期验证。详见input-coverage-audit.json。',
        '', '两期手续费加名义滑点的平均R差约0.0037，净R差约0.3413。费用放大问题，但解释不了主体差异。按原账本加回名义摩擦，上半年仍约−0.0426R；这是账面拆解，不是假设零成本重新回放的策略。',
        '', '## 冻结的观察组合','',
        '先要求60分钟方向同向；强反转/长影线预警使用分钟收盘确认后下一分钟开盘成交，累积覆盖预警沿用原触发。保留原S2止损、附近已确认结构目标及费用/机会竞争规则。',
        '', '|成交口径|每边滑点|上半年笔数|上半年净额|上半年平均R|近期净额|近期收益保留|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for suffix,e in stress['early'].items():
        r=stress['recent'][suffix];mode,slip=suffix.split('/')
        lines.append(f"|{mode}|{slip}|{e['n']}|{e['net']:.2f}|{e['mean_r']:.4f}|{r['net']:.2f}|{r['retention']:.1%}|")
    lines+=['',
        'next_minute_open是在各方案自身触发后再增加一分钟延迟的压力情景，不能当作真实下一报价撮合。R按实际成交到原止损的初始风险计算，金额按每手计算。两种加权不同，平均R为正时每手总金额仍可为负：净额=Σ(每笔净R×该笔风险金额)。',
        '', '不能晋升的原因：','',
        '- 两个2tick场景上半年每手净额仍为负，平均R余量很薄。',
        '- 近期相对同模式参考净额仅保留约6%—42%，低于80%。',
        '- 留一品种后，上半年四场景分别有3/6/1/5个剔除结果平均R转负；剔除最好3品种的现金结果均为负，集中依赖未解决。',
        '- 按预警周跨品种共同重采样的平均R区间跨零；该区间未校正多次研究。区间和全部留品种结果保留在原始产物。',
        '', '## 哪些思路本轮没有解决问题','',
        '单纯加确认、警报实体失效过滤、回踩站回及净空间门槛，均未单独消除上半年负R。在线30/60笔绩效停开减少亏损金额，却让保留下来交易的上半年平均R更负。统一1.25/1.5/2R兑现上限也没有改善为正期望，并显著截断近期盈利。下一轮不再靠扫描更密参数追求历史转正。',
        '', '## 下一步验证边界','',
        '观察组合已冻结于observation-candidate.json。优先补足上下文预热并记录逐报价成交，再用新增时期检验中间尺度方向条件；保留未过滤基线和所有被拒预警的纸面账本。此前约定至少200笔新增可定价平仓、60个新增交易日作为集中复核检查点，数量达标不自动通过。当前未启动采集、定时任务或交易服务。',
        '', '没有满足稳定目标就不把历史最优组合改成实盘配置，也不把两段已反复研究的历史称为独立验证。',
        '', '## 核验与文件','',
        '基线2997条接纳结果、成交与费用逐项回归一致；跨期合并没有改变参考机会集合。169个生产文件哈希未变。19项原/新Rust撮合测试、1项目标取整测试、3项上下文未来数据不变性测试、6项Python因果筛选测试通过。456704条新执行器评估，加上下文重筛的机会竞争与留品种重算；不是456704笔独立交易。',
        '', '原始批次：study-summary.md、monetization-supplement/、context-supplement/、context-combinations/；每批protocol.json、results.json、gates.json、accepted.jsonl、leave-product-out.json和verification.json均保留。',
        '', '后两类补充报告的通用“在线窗口”段落不适用于该补充分支，实际online_windows=[]，只以各自协议与此汇总为准。']
    (ROOT/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    assert all(c.sha(path)==h for path,h in c.read(ROOT/'protocol.json')['production_before'].items())
    c.write(ROOT/'completion.json',dict(status='research_round_complete_stability_not_achieved',production_changed=False,qualified_for_trading=False,
        unique_templates=27,passed_complete_gates=all_passed,primary_new_finding='Hourly direction environment and kind-specific confirmation give positive early meanR across4 scenarios, but cash/retention/concentration fail.',
        files={str(p.relative_to(ROOT)):c.sha(p) for p in [ROOT/'report.md',ROOT/'final-analysis.json',ROOT/'observation-candidate.json']}))
    print(json.dumps(dict(stability_achieved=False,early_average_r_positive_4_scenarios=True,candidate=stress,uncertainty=uncertainty),ensure_ascii=False,indent=2))

if __name__=='__main__':run()
