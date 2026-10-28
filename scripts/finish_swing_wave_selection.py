"""Freeze the parsimonious observation candidate and audit its improvement."""
import collections,datetime as dt,json,pathlib,statistics
import research_swing_wave_selection as w
import research_swing_abstop as s
from research_swing_combination import weekly_delta
RUN=w.RUN;NAME='near_start/adaptive'

def market_key(r):return r['symbol'],r['direction'],r['warning_ts']

def run():
    p=w.read(RUN/'protocol.json');res=w.read(RUN/'results.json');gates=w.read(RUN/'gates.json');assert gates[NAME]['defensive_dominates_previous']
    old=s.read(w.SOURCE/'full-portfolio.json');oldrows=collections.defaultdict(list);newrows=collections.defaultdict(list)
    for root,case,target in [(w.SOURCE,'P2_ab_adaptive',oldrows),(RUN,NAME,newrows)]:
        for line in (root/'accepted.jsonl').open(encoding='utf-8'):
            z=json.loads(line)
            if z['case'].split('/corrected_touch')[0]!=case and not z['case'].startswith(case+'/next_minute_open/'):continue
            prefix=case+'/'
            if z['case'].startswith(prefix):target[(z['policy'],z['period'],z['case'][len(prefix):])].append(z['row'])
    analyses={};stress={};uncertainty={};unknown={};contribution={}
    for policy in ('original','two_paths_flat'):
        analyses[policy]={};stress[policy]={}
        for q in ('early','recent'):
            analyses[policy][q]={};stress[policy][q]={}
            for mode in p['modes']:
                for slip in (1,2):
                    suffix=f'{mode}/slip{slip}';a=newrows[(policy,q,suffix)];b=oldrows[(policy,q,suffix)]
                    new={market_key(r):r for r in a if s.ev(r)['status']=='closed'};prior={market_key(r):r for r in b if s.ev(r)['status']=='closed'};common=new.keys()&prior.keys()
                    delta=sum(s.ev(new[k])['net_amount']-s.ev(prior[k])['net_amount'] for k in common);added=sum(s.ev(new[k])['net_amount'] for k in new.keys()-prior.keys());removed=sum(s.ev(prior[k])['net_amount'] for k in prior.keys()-new.keys())
                    m=res[policy][q][NAME+'/'+suffix];base=old[policy][q]['P2_ab_adaptive/'+suffix];assert abs(m['net']-base['net']-delta-added+removed)<1e-6
                    target_changed=[k for k in common if new[k]['target']!=prior[k]['target']];entry_changed=[k for k in common if s.ev(new[k])['fill']!=s.ev(prior[k])['fill'] or s.ev(new[k])['entry_ts']!=s.ev(prior[k])['entry_ts']]
                    analyses[policy][q][suffix]=dict(common=len(common),common_delta=delta,new_only=len(new.keys()-prior.keys()),new_only_net=added,old_only=len(prior.keys()-new.keys()),old_only_net=removed,
                        common_target_changed=len(target_changed),common_entry_changed=len(entry_changed),target_changed_delta=sum(s.ev(new[k])['net_amount']-s.ev(prior[k])['net_amount'] for k in target_changed))
                    stress[policy][q][suffix]=dict(net=m['net'],old_net=base['net'],mean_r=m['mean_r'],old_mean_r=base['mean_r'],n=m['n'],old_n=base['n'],win_rate=m['win_rate'],old_win_rate=base['win_rate'],
                        drawdown=m['realized_cash_drawdown'],old_drawdown=base['realized_cash_drawdown'],unknown=m['unknown'],old_unknown=base['unknown'],retention=m['net']/base['net'] if base['net']>0 else None,
                        without_best3products=m['without_best3products'],without_best3days=m['without_best3days'],tail10_cash=m['tail10_cash'],old_tail10_cash=base['tail10_cash'])
            if policy=='original':
                suffix='corrected_touch/slip1';a=newrows[(policy,q,suffix)];b=oldrows[(policy,q,suffix)];uncertainty[q]=weekly_delta(b,a)
                unknown[q]=dict(new=[market_key(r) for r in a if s.ev(r)['status'] in s.flat.s.UNKNOWN],old=[market_key(r) for r in b if s.ev(r)['status'] in s.flat.s.UNKNOWN])
                diff=collections.defaultdict(float)
                for sign,rs in [(1,a),(-1,b)]:
                    for r in rs:
                        if s.ev(r)['status']=='closed':diff[r['symbol']]+=sign*s.ev(r)['net_amount']
                contribution[q]=dict(sorted(diff.items(),key=lambda z:z[1],reverse=True))
    # Inspect wave semantics on the same warning times, independently of outcomes.
    changes={};durations={}
    pools={wave:{q:{(x['event']['symbol'],x['event']['direction'],x['event']['warning_ts']):x['event'] for x in w.read(RUN/wave/(q+'-pool.json'))} for q in ('early','recent')} for wave in ('legacy','near_start')}
    for q in ('early','recent'):
        oldp,newp=pools['legacy'][q],pools['near_start'][q];common=oldp.keys()&newp.keys();changed=[k for k in common if oldp[k]['s0_ts']!=newp[k]['s0_ts']]
        grade=lambda e:'A' if e['retracement']<=.5 else 'B' if e['retracement']<=.66 else 'C'
        changes[q]=dict(common=len(common),changed_s0=len(changed),mean_old_A_bars=statistics.mean(oldp[k]['a_bars'] for k in changed),mean_new_A_bars=statistics.mean(newp[k]['a_bars'] for k in changed),
            mean_old_retracement=statistics.mean(oldp[k]['retracement'] for k in changed),mean_new_retracement=statistics.mean(newp[k]['retracement'] for k in changed),grade_transitions=dict(collections.Counter(grade(oldp[k])+'->'+grade(newp[k]) for k in changed)))
        durations[q]={}
        for wave,rs in [('legacy',oldrows[('original',q,'corrected_touch/slip1')]),('near_start',newrows[('original',q,'corrected_touch/slip1')])]:
            es=[pools[wave][q][market_key(r)] for r in rs if s.ev(r)['status']=='closed'];durations[q][wave]=dict(n=len(es),mean_a_bars=statistics.mean(e['a_bars'] for e in es),mean_b_bars=statistics.mean(e['b_bars'] for e in es))
    interaction=w.read(RUN/'execution-interaction/results.json');inter=[]
    for profile in ('adaptive_b_strong','adaptive_b_all'):
        inter.append(dict(name=profile,early_net=interaction['original']['early']['near_start/'+profile+'/corrected_touch/slip1']['net'],recent_net=interaction['original']['recent']['near_start/'+profile+'/corrected_touch/slip1']['net']))
    candidate=dict(id=NAME,status='research_observation_only',qualified_for_trading=False,production_changed=False,selected_after_development_results=True,independent_holdout=False,
        definition=dict(wave='Keep existing confirmed fine/large swing windows and merging. Among current structurally qualified AB candidates select latest S0 FIRST, then original score/ties, same priority across levels. Final warning/cost/space checks still apply. Not a post-hoc replacement of losing trades.',
            execution='Keep previous strong_kind_session: aligned/missing context original touch; counter-hour strong/wick minute-close confirm, cumulative touch; strong-counter final-session10min restrict entries and exit gross losing holds next minute open.',
            preserved='Original S2 stop, local target RULE, fees, min10tick, netspace1, no-follow5, calendars, service forward dedup and trigger-time capacity. No added A breakout or new product/month thresholds.'),
        reason='Both near_start/adaptive and unmerged/adaptive pass defensive improvement vs prior observation in all4 scenarios under both exposure policies. Choose simpler existing pivot merging; unmerged contributes only one extra early losing-trade difference and no recent gain.',
        results=stress,uncertainty=uncertainty,
        remaining=['Early cash and meanR still negative in all4 scenarios','Remaining product/day dependence','Historical data repeatedly researched and incomplete symbol coverage','No tick/quote execution or full equity validation'],
        validation='Freeze this and the prior observation for new out-of-period/forward paper records; keep rejected warnings and both uncertain-PnL/exposure ledgers. Existing200closed/60new-trading-day checkpoint is not automatic qualification. Collection not started.',
        protocol_sha256=w.sha(RUN/'protocol.json'))
    w.write(RUN/'observation-candidate.json',candidate);w.write(RUN/'improvement-attribution.json',dict(paired=analyses,by_product_delta=contribution,uncertainty=uncertainty,unknown_identity=unknown,wave_changes=changes,accepted_wave_durations=durations,interaction=inter))
    early=stress['original']['early']['corrected_touch/slip1'];recent=stress['original']['recent']['corrected_touch/slip1']
    lines=['# 最近有效A：本轮可保留的改善','',
        '本轮采用完整再提取，找到比上一轮更好的观察版：最近合格A优先，执行沿用strong_kind_session。近期四场景收益均提高，上半年现金亏损、平均净R与回撤均改善，两种未知占用口径方向一致；上半年仍为负，未达到全周期稳定。生产代码和参数没有改动。',
        '', '|场景|上半年旧净额|新净额|旧平均R|新平均R|近期旧净额|新净额|近期保留|','|---|---:|---:|---:|---:|---:|---:|---:|']
    for suffix,e in stress['original']['early'].items():
        n=stress['original']['recent'][suffix];lines.append(f"|{suffix}|{e['old_net']:.2f}|{e['net']:.2f}|{e['old_mean_r']:.4f}|{e['mean_r']:.4f}|{n['old_net']:.2f}|{n['net']:.2f}|{n['retention']:.1%}|")
    lines+=['', '## 为什么这条比继续加确认更值得保留','',
        '原算法先选A/B评分最高的形态，时效通常在并列时参与；较远、较大的旧A可能让同一段回调显得更浅、更健康。新规则先选择最近满足既有结构资格的A，之后才计算警报、成本和可交易空间。',
        '', '对相同预警时间，早期1354条、近期593条的S0改变，而计划入场价/止损价没有改变。改变的是结构解释、S1/目标上限、评分与机会去重/占用顺序。本轮没有通过放大止损、提高仓位或事后改写亏损交易制造改善。',
        '', '变化样本中的A长度、回撤等级及交易差额详见improvement-attribution.json。不能把总差额全归于目标变近：必须同时报告共同交易的收益变化、新接纳机会和不再接纳机会。',
        '', '1tick配对核验：上半年422笔共同交易的成交收益完全相同；新纳入22笔净赚2922.55元，不再纳入30笔原净亏2018.04元，合计少亏4940.59元。近期159笔共同交易也完全相同，新19笔+3729.64元，未再纳入18笔原−563.39元，合计增加4293.02元。改善来自机会选择与竞争变化，不能解释为共同交易的反转预测更精准。',
        '', '发生S0变化的上半年预警子集中，A平均16.87→7.71根，回撤比例44.6%→56.5%；但全部接纳交易的平均A仅20.16→17.77根，B仍约13根。旧A可能使同一段回调看起来更浅；本轮尚未把策略变成更短时间尺度，也没有保证B已结束。',
        '', '## 对新增细节保持克制','',
        '不合并相近摆点的扩展，在1tick上半年仅额外少亏132元，近期四场景净额不增加；没有用这一点开发期差异升级为更多识别规则。最近S0与最近S1的两种排序在当前S1区间极值定义下输出逐字节相同，不当作两份证据。',
        '', '在较近A上继续给B未衰竭机会加确认，1tick上半年由−6863.29改善到−5918.30，但近期由21075.34降到16879.19；扩大到其他警报反而更差。因此保留较简单的near_start/adaptive，额外B条件不叠加。',
        '', '## 风险与边界','',
        f"- 1tick上半年已实现现金回撤从{early['old_drawdown']:.2f}降到{early['drawdown']:.2f}，仍有约{abs(early['net']):.0f}元已知平仓亏损。",
        f"- 近期1tick胜率从{recent['old_win_rate']:.2%}到{recent['win_rate']:.2%}，回撤从{recent['old_drawdown']:.2f}到{recent['drawdown']:.2f}；胜率没有因为重新识别就自动变高。",
        '- 2tick及额外一分钟延迟下，上半年平均R仍显著为负，不能因1tick接近零就称稳定。',
        '- 原占用口径下上半年未知0笔、近期未知1笔；新旧候选未知机会身份另行核对。未知盈亏未计为0。',
        '- 去掉最好3品种/日期、留一品种和已知交易周重采样均保留；新旧都依赖头部贡献。周区间未校正本轮及此前多次研究，不能拿开发期最优当泛化证据。',
        '- 近期增量中EG约+2030元、BU约+1681元、MA约+1320元，超过总增量4293元，其余品种合计拖累；收益分散问题未解决。新旧现金差的描述性预警周95%区间在两期均跨零，没有多重研究校正，不能称统计确认的稳定改善。',
        '- 8—10月子样本也保留，近期各品种历史起点不齐的缺口仍未补齐；本轮没有独立新数据验证。',
        '', '## 已完成的核验与交付','',
        '原识别与上一轮观察版共32个时期/压力/占用场景与归档一致。所有A模型重新提取全预警，再调用服务前向去重；生成过程中不构造未来盈亏标签。结构前缀测试和断档/午夜聚合测试通过。生产169个文件哈希不变。',
        '', '主实验16个不同模型，另有4个探索性交互模型；重复排序与重复控制不计作独立证据。全部失败结果保留。最终观察规则见observation-candidate.json，全场景和门槛见results.json/gates.json，归因见improvement-attribution.json。',
        '', '后续优先沿用此冻结版收集新时期证据，并继续区分“最近结构合格A”与“真实最近推动已被破坏后回看挑旧A”的状态。不能为了消掉剩余负R继续在同一批历史上堆条件。']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8')
    assert all(w.sha(k)==h for k,h in p['production_before'].items());main=w.read(RUN/'verification.json');assert len(main['archive_regressions'])==32 and all(x['exact'] for x in main['archive_regressions'])
    w.write(RUN/'completion.json',dict(status='research_round_complete_defensive_improvement_not_stable',qualified_for_trading=False,production_changed=False,production_unchanged=169,
        unique_main_models=16,additional_exploratory_models=4,selected=NAME,full_gates_passed=[],
        artifacts={str(f.relative_to(RUN)):w.sha(f) for f in [RUN/'summary.md',RUN/'report.md',RUN/'observation-candidate.json',RUN/'improvement-attribution.json',RUN/'results.json',RUN/'gates.json',RUN/'execution-interaction/results.json']},
        scripts={str(f.relative_to(w.REPO)):w.sha(f) for f in [pathlib.Path(__file__),w.REPO/'scripts/research_swing_wave_selection.py',w.REPO/'scripts/swing_wave_pool.rs',w.REPO/'scripts/research_swing_unmerged.py',w.REPO/'scripts/report_swing_wave_selection.py',w.REPO/'scripts/research_swing_wave_interaction.py']}))
    print(json.dumps(dict(candidate=NAME,primary=dict(early=early,recent=recent),attribution=analyses['original'],uncertainty=uncertainty,wave_changes=changes),ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':run()
