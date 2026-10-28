"""Explain EG/product PnL differences; descriptive identities, no fitted gates."""
import collections
import datetime as dt
import itertools
import json
import math
import statistics as st
import research_swing_specialists as s

RUN=s.ROOT/'research-runs/swing-eg-profit-drivers-20261026-v1'
READ=s.m.old.read
EV=s.m.report.s.ev
MONTHS=s.MONTHS
BLOCKS=[('2025MayAug','2025-05','2025-09'),('2025SepDec','2025-09','2026-01'),
        ('2026Q1','2026-01','2026-04'),('2026Q2','2026-04','2026-07'),
        ('2026Q3','2026-07','2026-10'),('2026Oct_partial','2026-10','2026-11')]

def write(name,z):s.m.old.write(RUN/name,z)
def fmt(v):return '—' if v is None else f'{v:,.2f}'

def load():
    c,pp,events,ends,data,sources,coverage=s.m.load();events=events['unmerged'];ends=ends['unmerged']
    p=READ(s.MAY/'protocol.json');cal=s.TradingDates({});cal.dates=READ(s.MAY/'trading-dates.json')
    older=s.m.report.row_cases(s.MAY/'older/evaluations.jsonl','unmerged','may_aug',READ(p['fees_path']),cal)
    pool=READ(s.MAY/'older-pool.json');events.update({z['event_key']:z['event'] for z in pool})
    for code,end in READ(s.MAY/'older/manifest.json')['contract_ends'].items():ends[code]=max(end,ends.get(code,''))
    rows=[z for z in data['unmerged']['budget_body',s.m.PRIMARY]+older['budget_body',s.m.PRIMARY] if z['symbol'] in s.FOUR]
    aa,ll,_=c.select(rows,events,ends,pp['sector_map'],'sector2_counter_stops','original');s.m.report.audit(ll,rows)
    assert all(EV(r)['status'] not in s.m.UNKNOWN for r in aa)
    trades=[r for r in aa if EV(r)['status']=='closed']
    ref=READ(s.MAY/'combined18-results.json')['original']['B5'][s.m.PRIMARY]['four_EG_V_JD_SR']
    assert len(trades)==ref['n'] and abs(sum(EV(r)['net_amount'] for r in trades)-ref['net'])<1e-7
    paths=[s.MAY/'older-pool.json',s.m.old.RUN/'unmerged/older-pool.json',s.NATIVE/'early-pool.json',s.NATIVE/'recent-pool.json']
    snaps={z['event_key']:z for path in paths for z in READ(path)}
    return trades,rows,events,snaps,ref

def describe(rows,events,snaps):
    es=[EV(r) for r in rows];n=len(rows);vals=[e['net_amount'] for e in es];rs=[e['net_r'] for e in es]
    risks=[e['risk']*r['multiplier'] for e,r in zip(es,rows)]
    plan=[r['planned_risk']*r['multiplier'] for r in rows]
    fee=[r['cost_repricing']['commission'] for r in rows]
    slip=[sum(f['volume'] for f in r['result']['fills'])*r['slip']*r['tick_size']*r['multiplier'] for r in rows]
    winners=[v for v in rs if v>0];losers=[-v for v in rs if v<0]
    p=len(winners)/n if n else None;w=st.mean(winners) if winners else None;l=st.mean(losers) if losers else None
    if n and w is not None and l is not None:assert abs(st.mean(rs)-(p*w-(1-p)*l))<1e-9
    monthly={mo:[] for mo in MONTHS};waves=collections.defaultdict(float);weeks=collections.defaultdict(float);exits=collections.defaultdict(list)
    for r in rows:
        monthly[r['warning_ts'][:7]].append(r)
        e=events[r['event_key']];wave='|'.join(e[k] for k in ['symbol','direction','s0_ts','s1_ts'])
        waves[wave]+=EV(r)['net_amount']
        d=dt.date.fromisoformat(r['warning_ts'][:10]);week=d-dt.timedelta(days=d.weekday())
        weeks[week.isoformat()]+=EV(r)['net_amount'];exits[EV(r)['exit_reason']].append(r)
    cash=peak=dd=0.;peak_at=None;ddperiod=None
    for r in sorted(rows,key=lambda r:(EV(r)['exit_ts'],r['event_key'])):
        cash+=EV(r)['net_amount']
        if cash>peak:peak=cash;peak_at=EV(r)['exit_ts']
        if peak-cash>dd:dd=peak-cash;ddperiod=[peak_at,EV(r)['exit_ts']]
    cash_values=sorted(vals,reverse=True);positive_months=[sum(EV(r)['net_amount'] for r in v) for v in monthly.values() if v]
    gross_before_cost=sum(vals)+sum(fee)+sum(slip)
    friction_r=[(a+b)/k for a,b,k in zip(fee,slip,risks)]
    z=dict(n=n,net=sum(vals),mean_net=st.mean(vals) if vals else None,
        mean_net_r=st.mean(rs) if rs else None,win_rate=p,mean_win_r=w,mean_loss_r=l,
        mean_cash_risk=st.mean(risks) if risks else None,cash_risk_weighted_mean_r=sum(vals)/sum(risks) if sum(risks) else None,
        normalized_planned_300_net=300*sum(v/k for v,k in zip(vals,plan)),
        mean_plan_net_r=st.mean(v/k for v,k in zip(vals,plan)) if plan else None,
        commission=sum(fee),slippage_accounting=sum(slip),total_friction_accounting=sum(fee)+sum(slip),
        fixed_path_gross_before_friction=gross_before_cost,
        friction_share_of_positive_path_net=(sum(fee)+sum(slip))/gross_before_cost if gross_before_cost>0 else None,
        mean_friction_r=st.mean(friction_r) if friction_r else None,
        fixed_path_mean_r_before_friction=st.mean(a+b for a,b in zip(rs,friction_r)) if rs else None,
        median_planned_target_r=st.median((r['target']-r['entry'])*(1 if r['direction']=='up' else -1)/r['planned_risk'] for r in rows) if rows else None,
        median_accepted_ATR_ticks=st.median(snaps[r['event_key']]['atr14']/r['tick_size'] for r in rows) if rows else None,
        median_risk_to_ATR=st.median(r['planned_risk']/snaps[r['event_key']]['atr14'] for r in rows) if rows else None,
        realized_cash_drawdown=dd,drawdown_dates=ddperiod,
        active_warning_months=len(positive_months),positive_warning_months=sum(v>0 for v in positive_months),
        negative_warning_months=sum(v<0 for v in positive_months),
        positive_weeks=sum(v>0 for v in weeks.values()),active_weeks=len(weeks),
        distinct_A_clusters=len(waves),positive_A_clusters=sum(v>0 for v in waves.values()),
        monthly={mo:dict(n=len(v),net=sum(EV(r)['net_amount'] for r in v)) for mo,v in monthly.items()},
        remove_top_trades={str(k):sum(vals)-sum(max(0,v) for v in cash_values[:k]) for k in [1,3,5,10]},
        remove_top_months={str(k):sum(vals)-sum(sorted([max(0,v) for v in positive_months],reverse=True)[:k]) for k in [1,2,3]},
        remove_top_A_clusters={str(k):sum(vals)-sum(sorted([max(0,v) for v in waves.values()],reverse=True)[:k]) for k in [1,3,5]},
        remove_top_weeks={str(k):sum(vals)-sum(sorted([max(0,v) for v in weeks.values()],reverse=True)[:k]) for k in [1,3,5]},
        top5_share_of_gross_wins=sum(max(0,v) for v in cash_values[:5])/sum(max(0,v) for v in vals) if any(v>0 for v in vals) else None,
        exits={reason:dict(n=len(v),net=sum(EV(r)['net_amount'] for r in v),mean_r=st.mean(EV(r)['net_r'] for r in v),
              contribution_to_overall_mean_r=sum(EV(r)['net_r'] for r in v)/n) for reason,v in exits.items()})
    return z

def run():
    RUN.mkdir(exist_ok=False)
    write('protocol.json',dict(created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        purpose='Descriptive profit-driver accounting, tail/month stability and feasibility of half-EG targets; no strategy tuning or new backtests.',
        identity='net = count * mean actual cash risk * cash-risk-weighted mean netR; also win probability*mean winR - loss probability*mean lossR.',
        friction='Undo already embedded slippage and fees on SAME observed accepted entries/exits for accounting only. Not a friction-free strategy rerun; qualification/fills/exits would otherwise change.',
        stability='18warning months, repeated historical research; do not infer independence from positive-month counts. Drop-winner diagnostics are not deployable strategies.',
        goals='Report each product half EG for same-period one-lot cash and300yuan PLANNED-risk fractional-lot normalization. Required count holds current observed mean fixed: arithmetic stress, not forecast.',
        production_before=READ(s.ARCHIVE/'protocol.json')['production_before'],script_sha256=s.m.old.sha(__file__)))
    trades,raw,events,snaps,ref=load();result={}
    for sym in s.FOUR:
        rows=[r for r in trades if r['symbol']==sym]
        z=describe(rows,events,snaps)
        source=[r for r in raw if r['symbol']==sym]
        z['post_score_candidates']=len(source);z['candidate_to_closed_rate']=len(rows)/len(source)
        z['median_all_candidate_ATR_ticks']=st.median(snaps[r['event_key']]['atr14']/r['tick_size'] for r in source)
        z['blocks']={name:describe([r for r in rows if start<=r['warning_ts'][:7]<end],events,snaps) for name,start,end in BLOCKS}
        result[sym]=z
        assert abs(z['net']-ref['products'][sym]['net'])<1e-7
        assert z['n']==ref['products'][sym]['n']
    eg=result['EG0'];decomposition={};goals={}
    for sym in ['V0','JD0','SR0']:
        z=result[sym]
        factors=dict(count_ratio=eg['n']/z['n'],mean_cash_risk_ratio=eg['mean_cash_risk']/z['mean_cash_risk'],
                     weighted_net_R_ratio=eg['cash_risk_weighted_mean_r']/z['cash_risk_weighted_mean_r'])
        ratio=math.prod(factors.values());assert abs(ratio-eg['net']/z['net'])<1e-7
        pp=(eg['win_rate']+z['win_rate'])/2
        effects=dict(win_rate=(eg['win_rate']-z['win_rate'])*((eg['mean_win_r']+z['mean_win_r']+eg['mean_loss_r']+z['mean_loss_r'])/2),
                     winning_payoff=(eg['mean_win_r']-z['mean_win_r'])*pp,
                     losing_severity=-(eg['mean_loss_r']-z['mean_loss_r'])*(1-pp))
        assert abs(sum(effects.values())-(eg['mean_net_r']-z['mean_net_r']))<1e-9
        gap=eg['mean_net_r']-z['mean_net_r'];pathgap=eg['fixed_path_mean_r_before_friction']-z['fixed_path_mean_r_before_friction']
        costadv=z['mean_friction_r']-eg['mean_friction_r'];assert abs(gap-pathgap-costadv)<1e-9
        decomposition[sym]=dict(multiplicative_factors=factors,total_net_ratio=ratio,
             candidate_supply_ratio=z['post_score_candidates']/eg['post_score_candidates'],
             conversion_ratio=z['candidate_to_closed_rate']/eg['candidate_to_closed_rate'],
             mean_R_gap=gap,mean_R_gap_components=effects,
             fixed_path_mean_R_gap_before_friction=pathgap,friction_advantage_R=costadv)
        goal=.5*eg['net'];normgoal=.5*eg['normalized_planned_300_net']
        goals[sym]=dict(one_lot_goal=goal,current_net=z['net'],goal_multiplier=goal/z['net'],
            current_trades=z['n'],required_trades_at_unchanged_mean=math.ceil(goal/z['mean_net']) if z['mean_net']>0 else None,
            required_mean_yuan_at_current_n=goal/z['n'],
            normalized_goal=normgoal,current_normalized=z['normalized_planned_300_net'],
            normalized_goal_multiplier=normgoal/z['normalized_planned_300_net'] if z['normalized_planned_300_net']>0 else None,
            required_trades_at_unchanged_normalized_mean=math.ceil(normgoal/(300*z['mean_plan_net_r'])) if z['mean_plan_net_r']>0 else None)
    stress=READ(s.MAY/'combined18-results.json');stress_summary={}
    for sym in s.FOUR:
        cases=[stress[pol]['B5'][sf]['four_EG_V_JD_SR']['products'][sym] for pol in ['original','two_paths_flat'] for sf in s.m.SUFFIXES]
        stress_summary[sym]=dict(min_known_net=min(z['net'] for z in cases),max_unknown=max(z['unknown'] for z in cases),
            months_all_cases_positive=[mo for mo in MONTHS if all(z['warning_month_net'][mo]>0 for z in cases)],
            monthly_known_net_min={mo:min(z['warning_month_net'][mo] for z in cases) for mo in MONTHS})
    recent=READ(s.ROOT/'research-runs/swing-incremental-edge-20261026-v1/test-results.json')['original/'+s.m.PRIMARY]['B5_original']['products']
    write('results.json',dict(products=result,decomposition=decomposition,half_EG_goals=goals,stress=stress_summary,recent_AprOct=recent))
    with (RUN/'closed-trades.jsonl').open('x',encoding='utf-8') as f:
        for r in trades:f.write(json.dumps(r,ensure_ascii=False)+'\n')
    report(result,decomposition,goals,stress_summary,recent)
    chart(result)
    p=READ(RUN/'protocol.json');diff=[q for q,h in p['production_before'].items() if s.m.old.sha(s.ROOT/q)!=h];assert not diff
    write('verification.json',dict(exact_continuous_B5_closed=250,all_four_product_reference_nets_match=True,
         exact_count_risk_edge_factorization=True,exact_win_payoff_loss_decomposition=True,
         exact_fixed_path_friction_accounting=True,production_hash_differences=diff))
    write('completion.json',dict(complete=True,new_strategy_backtests=0,production_changed=False,
          artifacts={q.name:s.m.old.sha(q) for q in RUN.iterdir() if q.is_file()}))
    print(json.dumps(dict(EG={k:eg[k] for k in ['net','n','active_warning_months','positive_warning_months','remove_top_trades','remove_top_months','remove_top_A_clusters','remove_top_weeks']},
        factors=decomposition,goals=goals,friction={sym:{k:z[k] for k in ['mean_win_r','mean_loss_r','mean_net_r','mean_friction_r','fixed_path_gross_before_friction','total_friction_accounting','normalized_planned_300_net']} for sym,z in result.items()}),ensure_ascii=False),flush=True)

def report(result,decomp,goals,stress,recent):
    eg=result['EG0']
    lines=['# EG利润来源、稳定性与其他品种半数目标','',
        '日期：2026-10-26（北京时间）。固定四品种原B5、2025-05至2026-10已有18个预警月份，10月截至23日、近期品种覆盖不齐。金额为一手扣冻结手续费及滑点后的已知平仓净额。没有新增策略回测或改生产。',
        '', '## 1. 核心判断','',
        'EG的高收益是交易频率、每手现金风险和风险单位内盈利能力共同作用。相对鸡蛋，频率差异最重要；相对PVC，风险单位内的目标兑现优势更重要；相对白糖，当前信号和退出组合的毛优势薄、成本侵蚀高。不能用“EG波动大”统一解释。',
        '', f"EG在17个有平仓的月份里14个月盈利，删除最佳一笔后仍盈利{fmt(eg['remove_top_trades']['1'])}元，删除前5笔后仍盈利{fmt(eg['remove_top_trades']['5'])}元；这不是仅靠一两笔撑起来。但利润有明显右尾和阶段集中，删除最佳两个预警月份后剩{fmt(eg['remove_top_months']['2'])}元，2026Q2仍亏损。可称本样本月度盈利面广，尚不能推成未来稳定收益保证。",
        '', '## 2. 收益差距的精确拆解','',
        '恒等式：净额＝交易笔数×平均实际初始止损现金风险×现金风险加权平均净R。最后一项为总净额／累计初始现金风险，区别于每笔净R的简单平均；它同时反映大风险交易是否更容易亏损。',
        '', '|品种|平仓|净额|平均单笔净额|平均现金风险|平均净R|现金风险加权净R|', '|---|---:|---:|---:|---:|---:|---:|']
    for sym,z in result.items():lines.append(f"|{s.m.NAMES[sym]}|{z['n']}|{fmt(z['net'])}|{fmt(z['mean_net'])}|{fmt(z['mean_cash_risk'])}|{z['mean_net_r']:.3f}|{z['cash_risk_weighted_mean_r']:.3f}|")
    lines+=['','|EG相对品种|交易数倍数|平均现金风险倍数|加权净R倍数|最终净收益倍数|', '|---|---:|---:|---:|---:|']
    for sym,z in decomp.items():
        q=z['multiplicative_factors'];lines.append(f"|{s.m.NAMES[sym]}|{q['count_ratio']:.2f}|{q['mean_cash_risk_ratio']:.2f}|{q['weighted_net_R_ratio']:.2f}|{z['total_net_ratio']:.2f}|")
    lines+=['', '这些倍数相乘精确得到净额之比，并不是回归因果系数。一手EG本来承担更多现金风险，因此不能把全部现金差异视作信号更好；但风险单位内的差异仍然显著，尤其PVC/SR。',
        '', '### 2.1 鸡蛋：预警供给和转化率的双重压缩','', '|品种|评分后可回放候选|最终平仓|候选到平仓比例|', '|---|---:|---:|---:|']
    for sym,z in result.items():lines.append(f"|{s.m.NAMES[sym]}|{z['post_score_candidates']}|{z['n']}|{z['candidate_to_closed_rate']:.1%}|")
    lines+=['', '鸡蛋的候选数量约为EG的58%，候选转化率约为EG的37%，两者相乘使成交数仅约22%。这包含日间交易时段、实际覆盖、形态供给以及风险/成本/目标门槛，不能将全部差异归因于某一项。此前漏斗显示JD/SR主要收缩发生在计划资格而非B5最后的组合控制。',
        '', '18个月鸡蛋平均单笔现金净额约90元，已略高于EG约170元的一半；其累计净额不足，确实有频率因素。但2026年4—10月8笔仅约90元、平均净R0.007，说明旧样本的单笔质量也没有稳定延续，不能直接按旧均值机械放大笔数。',
        '', '## 3. EG主要赢在盈利端，止损并未特别神奇','',
        '平均净R＝胜率×平均盈利R−败率×平均亏损R。',
        '', '|品种|胜率|平均盈利R|平均亏损R绝对值|目标退出率|目标退出平均净R|', '|---|---:|---:|---:|---:|---:|']
    for sym,z in result.items():
        t=z['exits'].get('target_s1',dict(n=0,mean_r=0))
        lines.append(f"|{s.m.NAMES[sym]}|{z['win_rate']:.1%}|{z['mean_win_r']:.3f}|{z['mean_loss_r']:.3f}|{t['n']/z['n']:.1%}|{t['mean_r']:.3f}|")
    lines+=['', '|品种|目标退出对总平均净R贡献|原stop贡献|其他退出合计贡献|总平均净R|', '|---|---:|---:|---:|---:|']
    for sym,z in result.items():
        t=z['exits']['target_s1']['contribution_to_overall_mean_r'];stop=z['exits']['stop']['contribution_to_overall_mean_r']
        lines.append(f"|{s.m.NAMES[sym]}|{t:.3f}|{stop:.3f}|{z['mean_net_r']-t-stop:.3f}|{z['mean_net_r']:.3f}|")
    lines+=['', 'EG比PVC更常到达目标，达到目标时兑现的R也更高；EG与白糖的止损拖累相近，主要差距仍在盈利端。这比“止损更少”或“只因绝对波幅大”更接近直接的收益机制。`target_s1`是引擎退出标签，也包含局部截近目标，不能只凭标签理解成全部到达原S1。',
        '', '### 3.1 EG与其他品种平均净R差异的会计分配','', '|EG减去该品种|总净R差|胜率差贡献|盈利幅度差贡献|亏损幅度差贡献|', '|---|---:|---:|---:|---:|']
    for sym,z in decomp.items():
        q=z['mean_R_gap_components'];lines.append(f"|{s.m.NAMES[sym]}|{z['mean_R_gap']:.3f}|{q['win_rate']:.3f}|{q['winning_payoff']:.3f}|{q['losing_severity']:.3f}|")
    lines+=['', '分配采用两组平均权重的对称拆分，三项精确相加，无先后顺序偏差；它解释已发生的收益算术，不证明改变胜率或目标就能独立带来同样效果。',
        '', '## 4. 波动与成本：EG优势不只是更大的现金振幅','',
        '|品种|所有候选ATR中位/tick|同一成交路径扣摩擦前净额|手续费＋滑点还原额|最终净额|摩擦占还原前净额|', '|---|---:|---:|---:|---:|---:|']
    for sym,z in result.items():lines.append(f"|{s.m.NAMES[sym]}|{z['median_all_candidate_ATR_ticks']:.2f}|{fmt(z['fixed_path_gross_before_friction'])}|{fmt(z['total_friction_accounting'])}|{fmt(z['net'])}|{z['friction_share_of_positive_path_net']:.1%}|")
    lines+=['', 'EG与PVC候选的15分钟ATR tick中位接近；每tick金额和最终被选中的风险宽度不同。EG相对白糖、鸡蛋波动空间更大，但其全部收益差距无法由波动解释。白糖在本策略已接受路径中留下的摩擦前净额很薄，成本会吞掉其中很大部分。',
        '', '**这里是固定既有入场、离场和占用的摩擦会计还原，不是无摩擦回测。** 真正改费用或滑点会改变资格、触发、退出和竞争；不能将还原净额当作低费率策略可赚金额。',
        '', '|EG减去该品种|平均净R差|固定路径去摩擦后差|摩擦差带来的优势|', '|---|---:|---:|---:|']
    for sym,z in decomp.items():lines.append(f"|{s.m.NAMES[sym]}|{z['mean_R_gap']:.3f}|{z['fixed_path_mean_R_gap_before_friction']:.3f}|{z['friction_advantage_R']:.3f}|")
    lines+=['', '这张表区分毛路径优势和摩擦优势。即使做同路径摩擦还原，EG的领先也没有全部消失。因此单纯降低费用、缩止损或增加交易次数，并不能填平PVC/SR的差距。',
        '', '## 5. 是不是只抓到了少数大波','',
        f"EG共{eg['n']}笔，{eg['distinct_A_clusters']}个A簇，{eg['active_weeks']}个有平仓预警周、其中{eg['positive_weeks']}周净盈利；月份为{eg['positive_warning_months']}/{eg['active_warning_months']}个活跃月盈利。A簇和周并非独立的宏观行情样本，同一趋势可产生多个簇。",
        '', '|EG集中度压力诊断|剩余净额/元|', '|---|---:|']
    for k,v in eg['remove_top_trades'].items():lines.append(f'|删除最大{k}笔盈利|{fmt(v)}|')
    for k,v in eg['remove_top_months'].items():lines.append(f'|删除最佳{k}个预警月份|{fmt(v)}|')
    for k,v in eg['remove_top_A_clusters'].items():lines.append(f'|删除最佳{k}个A簇|{fmt(v)}|')
    lines+=['',f"前5笔约占所有盈利交易盈利总和的{eg['top5_share_of_gross_wins']:.1%}。净利润已扣掉亏损，所以‘前5笔占净利润’的比例会更高，不能据此说其余交易完全无贡献。删赢家只是尾部依赖诊断，不是可实施的入场规则。",
        '', '|连续计分组|EG净额|平均净R|笔数|', '|---|---:|---:|---:|']
    for name,_,_ in BLOCKS:
        z=eg['blocks'][name];lines.append(f"|{name}|{fmt(z['net'])}|{z['mean_net_r']:.3f}|{z['n']}|")
    lines+=['',f"EG主场景14个正收益月，但在全部8场景均为已知净额正的月份为{len(stress['EG0']['months_all_cases_positive'])}个；其8场景最低18个月已知净额为{fmt(stress['EG0']['min_known_net'])}元，最大未知笔数{stress['EG0']['max_unknown']}。不能把14个主场景正月当成对每种成交条件都稳定。",
        '', '## 6. 更深一层：哪些是证据，哪些仍是解释假设','',
        '已经有证据的链条是：原B5在EG更频繁保留下可成交空间，目标兑现端贡献更强，摩擦占比更低；利润虽有右尾，但删去数笔/数月后仍为正。前一轮保留原计划的匹配随机时点中，EG主场景约94百分位、PVC约98.5百分位，也支持“时点与结构的配合”不完全等同于随便参与上涨市场。',
        '', '更深的产业解释只能作为假设。华泰期货2026年7月30日专题报告记录了进口收缩、低库存及现实供给偏紧，说明那段EG可能拥有持续的供需驱动，近月与远月预期还有差异。这样的环境可能让“推动—回撤—续航”的交易空间更充足。但该资料不能解释全部2025—2026收益，也没有作为时点一致的特征纳入本轮模型，不能称为已证实的收益因果。[华泰期货原报告](https://htfc.com/main/a/20260730/80181158.shtml)。',
        '', '还没有证明的是：EG在不同时代/供需环境都维持该优势，或者哪一个形态特征单独带来超额。EG/PVC的完整规则有条件历史支持；把ABC信号换成统一ATR/2R规则后无优势，说明结构、空间门槛和执行相互依赖，不能将它归结为一个通用形态标签。',
        '', '## 7. “每个品种达到EG一半”的可行性','',
        '先同时列两种口径：同期间各一手现金净额，以及每机会计划风险300元的分数手研究归一值。后者不是账户收益或直接可下单手数，也没有统一各品种的累计换手与占用时间。',
        '', '|品种|当前一手净额|EG一半目标|维持当前平均单笔所需平仓数|现有笔数|维持现有笔数所需平均单笔净额|', '|---|---:|---:|---:|---:|---:|']
    for sym,g in goals.items():lines.append(f"|{s.m.NAMES[sym]}|{fmt(g['current_net'])}|{fmt(g['one_lot_goal'])}|{g['required_trades_at_unchanged_mean']}|{g['current_trades']}|{fmt(g['required_mean_yuan_at_current_n'])}|")
    lines+=['', '上述笔数只是“平均收益永不下降”的算术；不是放松后会获得这些笔数的预测。被排除机会未必有原成交质量，增加白糖交易甚至可能放大亏损。以本历史事后最强品种为标尺，其一半也不是天然容易或保守的目标。',
        '', '|品种|当前300计划风险归一净额|归一EG一半|保持当前归一单笔质量所需笔数|', '|---|---:|---:|---:|']
    for sym,g in goals.items():lines.append(f"|{s.m.NAMES[sym]}|{fmt(g['current_normalized'])}|{fmt(g['normalized_goal'])}|{g['required_trades_at_unchanged_normalized_mean']}|")
    lines+=['', '换成同风险口径后，距离可能缩小，但不能通过增加手数声称策略预测能力改善。比较时还要同时限制回撤、资金占用和执行压力，并按同一覆盖期计算。',
        '', '### 7.1 最近后续计分段会给出不同的目标难度','', '|2026年4—10月已有覆盖|笔数|净额|平均实际净R|', '|---|---:|---:|---:|']
    for sym,z in recent.items():lines.append(f"|{s.m.NAMES[sym]}|{z['n']}|{fmt(z['net'])}|{z['mean_net_r']:.3f}|")
    lines+=['', 'PVC近期的单笔净R接近EG，鸡蛋/白糖近期却接近零。这是不能直接把18个月历史均值外推、或仅按累计金额给品种打永久标签的原因。',
        '', '## 8. 下一步应做什么','',
        '### EG/PVC：保留完整B5，转向风险与状态验证','',
        '保留完整定义，不再为提高既有历史净额微调形态。检查盈利端能否在新行情延续、弱月份是否有可事前识别的状态，并用计划现金风险约束两者共同敞口。PVC近期有独立于EG单一走势的条件支持，但仍是化工相关来源。风险归一化或放大PVC手数不能自动等于利润来源分散。',
        '', '### JD：先验证空间与持仓尺度，再谈多交易','',
        '第一优先是保留原结构目标空间、减少局部目标过早截近，补齐既有S1/成本分支在旧月份的验证；不是直接再放宽成本比。此前2026观察分支中JD“20%＋原S1”20笔约2,107元，仍未达到同年EG约11,244元的一半，且已参与过历史选择，不能当成已验证解法。',
        '', '若其增量不能迁移，再独立预先定义更慢识别/跨日持仓的低换手结构分支，检查净空间相对费用是否改善。不要同时修改周期、入场、止损和目标后只保留最高曲线。季节/合约期限分支需要多个年度和时点可用数据，18个月不足以充分拟合。',
        '', '### SR：先寻找不同机制，不能把同一模板低收益仅当参数问题','',
        '先审计失败突破是否经常回到原区间、顺向行情是否主要被小波幅/成本侵蚀。根据审计只保留少数机制分支，例如更慢的趋势续航，或可明确界定区间内的失败突破回归；它们都只是待测假设，本轮没有回测验证。白糖当前毛优势太薄，降低门槛或放大手数不是优先方案。',
        '', '### 共用基础设施，少量机制分支，避免每品种拟合一套最优参数','',
        '行情清洗、换月、费用、占用、实际成交、仓位和报告保持共用；允许信号机制、目标空间和持仓尺度有少量不同分支。每轮先固定候选和评价，再按时间向前测试，报告失败分支。品种需要不同机制，不等于每个品种都必然存在可实现的高收益方案。',
        '', '### 把一半收益设为验收目标，分两步通过','',
        '第一步先要求扣费后正期望能够跨组延续、执行压力不过度脆弱，并且相对简单或条件随机基准有增量；样本稀疏时保留不确定性。第二步才在同风险、同覆盖和可接受回撤下评估能否达到EG一半。该目标不作为在旧历史中不断扫描参数直到命中的选优条件。若没有可泛化优势，应允许该品种少交易或不交易。',
        '', '## 9. 证据','',
        '[results.json](results.json)含精确乘法拆解、胜率/盈亏幅度拆分、摩擦会计还原、删尾部/月份/A簇压力及两类半数目标；[closed-trades.jsonl](closed-trades.jsonl)为连续重竞争后250笔已知平仓；[verification.json](verification.json)记录对照和恒等式核验。所有结果仍来自被反复研究的历史，不是独立新样本。',
        '', '![EG稳定性、目标兑现与摩擦结构](comparison.png)','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8')

def chart(result):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','SimHei','DejaVu Sans'],'axes.unicode_minus':False})
    fig,axes=plt.subplots(2,2,figsize=(13,9),layout='constrained',facecolor='#f8fafc');eg=result['EG0']
    ax=axes[0,0];vals=[eg['monthly'][mo]['net'] for mo in MONTHS]
    ax.bar(range(18),vals,color=['#0d9488' if v>0 else '#dc2626' if v<0 else '#cbd5e1' for v in vals]);ax.axhline(0,color='#475569',lw=.7)
    ax.set_xticks(range(18),[mo[2:] for mo in MONTHS],rotation=60,ha='right');ax.set_ylabel('一手扣费净额 / 元');ax.set_title('EG：17个活跃月中14个月正，但阶段贡献不均',loc='left',fontsize=11)
    ax=axes[0,1];keys=['原结果','去最大1笔','去最大3笔','去最大5笔','去最大10笔'];vals=[eg['net']]+[eg['remove_top_trades'][str(k)] for k in [1,3,5,10]]
    bs=ax.bar(range(5),vals,color='#2563eb');ax.bar_label(bs,fmt='%.0f',padding=3);ax.set_xticks(range(5),keys,rotation=20);ax.axhline(0,color='#475569',lw=.7);ax.set_ylabel('剩余已知净额 / 元');ax.set_title('有右尾依赖，仍不只是单笔偶然盈利',loc='left',fontsize=11)
    ax=axes[1,0];x=np.arange(4)
    target=[result[sym]['exits']['target_s1']['contribution_to_overall_mean_r'] for sym in s.FOUR]
    stop=[result[sym]['exits']['stop']['contribution_to_overall_mean_r'] for sym in s.FOUR]
    other=[result[sym]['mean_net_r']-a-b for sym,a,b in zip(s.FOUR,target,stop)]
    ax.bar(x,target,color='#0d9488',label='目标退出贡献');ax.bar(x,stop,color='#dc2626',label='原stop拖累');ax.scatter(x,[result[sym]['mean_net_r'] for sym in s.FOUR],color='#0f172a',s=45,label='全部退出后的平均净R',zorder=3)
    ax.set_xticks(x,['EG','PVC','JD','SR']);ax.axhline(0,color='#475569',lw=.7);ax.set_ylabel('对平均净R的贡献');ax.set_title('差异主要在目标兑现端，止损并不特别占优',loc='left',fontsize=11);ax.legend(frameon=False,fontsize=8)
    ax=axes[1,1];net=[result[sym]['net'] for sym in s.FOUR];cost=[result[sym]['total_friction_accounting'] for sym in s.FOUR]
    ax.bar(x,net,color='#2563eb',label='扣摩擦后净额');ax.bar(x,cost,bottom=net,color='#f59e0b',label='固定路径摩擦还原额')
    ax.set_xticks(x,['EG','PVC','JD','SR']);ax.set_ylabel('一手金额 / 元');ax.set_title('SR/V的毛路径优势更易被摩擦侵蚀',loc='left',fontsize=11);ax.legend(frameon=False,fontsize=8)
    for ax in axes.flat:ax.grid(axis='y',alpha=.14);ax.spines[['top','right']].set_visible(False)
    fig.suptitle('EG收益领先的结构：机会、现金风险、盈利质量与成本',fontweight='bold',fontsize=17)
    fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)

if __name__=='__main__':run()
