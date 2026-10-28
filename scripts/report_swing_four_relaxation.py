"""Attribute rejected opportunities separately from re-competed portfolio returns."""
import collections
import json
import pathlib
import statistics
import sys

sys.dont_write_bytecode = True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import research_swing_four_relaxation as r
from reprice_signal_costs import reprice, TradingDates

m=r.m
RUN=r.RUN
SOURCE=r.RUN
EVALUATION_PERIODS=['older','early','recent']
SPAN='2025年9月—2026年10月23日'
GATES={'filtered_geometry','filtered_entry_economics','filtered_A_net_body','filtered_net_rr_plan'}
LABELS={
    'C_control':'B5原规则', 'E_cost_space20':'目标成本比10%→20%',
    'F_risk8_friction30':'止损10→8tick、成本0.2→0.3R',
    'Q_net_rr08':'最低净盈亏比1→0.8', 'L_original_S1':'取消局部目标截近，保留原S1',
    'M_mild_combination':'前三项同时适度放松',
    'D_unrestricted_diagnostic':'仅用于给被过滤机会补做回放',
}


def write(name,obj):
    r.write(RUN/name,obj)


def metric(rows,events):
    z=m.report.distribution(rows,events,m.MONTHS)
    keys=['n','wins','losses','flat','win_rate','net','mean','mean_r','profit_sum','loss_sum',
          'realized_cash_drawdown','unknown','realized_gains','realized_losses','profit_factor',
          'warning_month_net','positive_warning_months','negative_warning_months','worst_warning_month','status_counts']
    return {k:z[k] for k in keys}


def delta(new,old,events):
    a={x['event_key']:x for x in new if m.report.s.ev(x)['status']=='closed'}
    b={x['event_key']:x for x in old if m.report.s.ev(x)['status']=='closed'}
    added=[a[k] for k in a.keys()-b.keys()]
    removed=[b[k] for k in b.keys()-a.keys()]
    common=sum(m.report.s.ev(a[k])['net_amount']-m.report.s.ev(b[k])['net_amount'] for k in a.keys()&b.keys())
    total=sum(m.report.s.ev(x)['net_amount'] for x in new if m.report.s.ev(x)['status']=='closed')-sum(m.report.s.ev(x)['net_amount'] for x in old if m.report.s.ev(x)['status']=='closed')
    am=metric(added,events);bm=metric(removed,events)
    assert abs(total-(am['net']-bm['net']+common))<1e-6
    return dict(net_delta=total,count_delta=len(a)-len(b),added=am,removed=bm,common_trade_net_change=common,
                added_keys=sorted(a.keys()-b.keys()),removed_keys=sorted(b.keys()-a.keys()))


def run(only_2026=False):
    global RUN,EVALUATION_PERIODS,SPAN
    if only_2026:
        RUN=SOURCE/'2026-only'
        RUN.mkdir(exist_ok=False)
        EVALUATION_PERIODS=['early','recent']
        SPAN='2026年1月—10月23日（仅已完成回放的2026年）'
        m.MONTHS=m.report.OLD_MONTHS
    p=m.old.read(SOURCE/'protocol.json')
    controller,pp,events,ends,original,sources,coverage=m.load()
    if only_2026:
        for family in original:
            for key,rs in original[family].items():original[family][key]=[z for z in rs if z['period']!='older']
        write('all-product-counts.json',m.old.read(SOURCE/'all-product-counts.json'))
        write('scope.json',dict(replay_periods=EVALUATION_PERIODS,all_product_counts_span='2025-09 through 2026-10-23',
            relaxation_span='2026-01 through 2026-10-23',missing_older_reason='Normal authenticated SDK initialization timed out repeatedly; no new2025 outcomes inferred or inserted. Original2025 results remain immutable.',
            portfolio_start='2026-01 clean start, matching frozen prior_2026 control'))
        write('protocol.json',dict(p,evaluation_periods=EVALUATION_PERIODS,source_protocol=str(SOURCE/'protocol.json'),
            source_protocol_sha256=m.old.sha(SOURCE/'protocol.json')))
    fees=m.old.read(p['fees_path'])
    new=collections.defaultdict(list);qa=collections.Counter();checks=[]
    base={}
    for sf in m.SUFFIXES:
        base[sf]={z['event_key']:z for z in original['unmerged']['budget_body',sf]}
    for period in EVALUATION_PERIODS:
        if period=='older':
            calendar=TradingDates({});calendar.dates=m.old.read(m.old.RUN/'trading-dates.json')
        else:
            calendar=TradingDates(m.old.read(p['periods'][period]['input'])['symbols'])
        path=SOURCE/period/'executions/evaluations.jsonl'
        assert (path.parent/'manifest.json').exists(),'Execution not complete: '+period
        sources[str(path)]=m.old.sha(path)
        for line in path.open(encoding='utf-8'):
            z=reprice(json.loads(line),fees,calendar)
            variant,sf=z['scenario'].split('/',1)
            z['period']=period
            if m.report.s.ev(z)['status']=='closed':
                z['exit_trading_day']=calendar.day(m.report.s.ev(z)['exit_ts'])
                assert z['result']['fills'][0]['ts']>z['warning_ts']
                for f in z['result']['fills']:
                    assert abs(f['price']/z['tick_size']-round(f['price']/z['tick_size']))<1e-6
                qa['closed_fill_tick_time_checks']+=1
            if variant=='C_control':
                expected=base[sf][z['event_key']]
                for field in ['result','entry','stop','target','planned_risk','new_friction','economic_pass','body_net_pass','cost_repricing']:
                    assert z.get(field)==expected.get(field),(period,sf,z['event_key'],field)
                qa['exact_frozen_control_rows']+=1
            new[variant,sf].append(z)
    assert qa['exact_frozen_control_rows']==sum(len([z for z in rs.values() if z['symbol'] in r.SYMS]) for rs in base.values())
    evs=events['unmerged'];period_focus={}
    results={};accepted={};ledgers={}
    for pol in p['policies']:
        results[pol]={}
        for name in r.VARIANTS:
            if name.startswith('D_'):continue
            results[pol][name]={}
            for sf in m.SUFFIXES:
                rows=[z for z in original['unmerged']['budget_body',sf] if z['symbol'] not in r.SYMS]+new[name,sf]
                assert len({z['event_key'] for z in rows})==len(rows)
                aa,ll,risk=controller.select(rows,evs,ends['unmerged'],pp['sector_map'],'sector2_counter_stops',pol)
                m.report.audit(ll,rows)
                focus=[z for z in aa if z['symbol'] in r.SYMS]
                metrics=dict(all=metric(aa,evs),focus=metric(focus,evs),products={s:metric([z for z in focus if z['symbol']==s],evs) for s in r.SYMS},
                    periods={q:metric([z for z in focus if z['period']==q],evs) for q in EVALUATION_PERIODS},
                    portfolio_risk={k:v for k,v in risk.items() if k!='realized_settlements'})
                results[pol][name][sf]=metrics
                accepted[pol,name,sf]=aa
                if name=='C_control':
                    expected=m.old.read(m.old.RUN/'results.json')[pol]['prior_2026' if only_2026 else 'combined']['B5/'+sf]
                    for k,v in metrics['all'].items():
                        if k in expected:assert m.equal(v,expected[k]),(pol,sf,k,v,expected[k])
                    checks.append(dict(policy=pol,scenario=sf,full_B5_exact=True))
                if pol=='original' and sf==m.PRIMARY:
                    rev,_,_=controller.select(rows,evs,ends['unmerged'],pp['sector_map'],'sector2_counter_stops',pol,reverse=True)
                    metrics['reverse_tie_focus_net']=metric([z for z in rev if z['symbol'] in r.SYMS],evs)['net']
                    ledgers[name]=ll
    attribution={}
    for name in results['original']:
        attribution[name]={}
        for pol in p['policies']:
            attribution[name][pol]={}
            for sf in m.SUFFIXES:
                aa=accepted[pol,name,sf];control=accepted[pol,'C_control',sf]
                attribution[name][pol][sf]=dict(
                    all=delta(aa,control,evs),
                    focus=delta([z for z in aa if z['symbol'] in r.SYMS],[z for z in control if z['symbol'] in r.SYMS],evs),
                    products={s:delta([z for z in aa if z['symbol']==s],[z for z in control if z['symbol']==s],evs) for s in r.SYMS})
    diagnostics={};diagnostic_rows=[]
    for sf in m.SUFFIXES:
        control={z['event_key']:z for z in new['C_control',sf]}
        diagnostics[sf]={}
        for sym in r.SYMS:
            blocked=[z for z in new['D_unrestricted_diagnostic',sf] if z['symbol']==sym and m.report.s.ev(control[z['event_key']])['status'] in GATES]
            diag=dict(total_rejected_candidates=len(blocked),outcomes=metric(blocked,evs),
                diagnostic_statuses=dict(collections.Counter(m.report.s.ev(z)['status'] for z in blocked)),
                by_original_gate={})
            for gate in sorted(GATES):
                rs=[z for z in blocked if m.report.s.ev(control[z['event_key']])['status']==gate]
                diag['by_original_gate'][gate]=dict(rejected=len(rs),outcomes=metric(rs,evs),statuses=dict(collections.Counter(m.report.s.ev(z)['status'] for z in rs)))
            diagnostics[sf][sym]=diag
            if sf==m.PRIMARY:
                diagnostic_rows.extend(dict(original_filter=m.report.s.ev(control[z['event_key']])['status'],row=z) for z in blocked)
        for z in new['D_unrestricted_diagnostic',sf]:
            if m.report.s.ev(control[z['event_key']])['status']=='closed':
                assert z['result']==control[z['event_key']]['result']
                qa['diagnostic_preserves_existing_closed_paths']+=1
    stress={}
    for name in results['original']:
        changes=[attribution[name][pol][sf]['focus']['net_delta'] for pol in p['policies'] for sf in m.SUFFIXES]
        stress[name]=dict(focus_net_delta_min=min(changes),focus_net_delta_max=max(changes),positive_cases=sum(x>1e-7 for x in changes),cases=len(changes))
    write('results.json',results)
    write('attribution.json',attribution)
    write('rejected-outcomes.json',diagnostics)
    write('stress-summary.json',stress)
    product_stress={name:{s:dict(
        positive_cases=sum(attribution[name][pol][sf]['products'][s]['net_delta']>1e-7 for pol in p['policies'] for sf in m.SUFFIXES),
        cases=8,
        min_net_delta=min(attribution[name][pol][sf]['products'][s]['net_delta'] for pol in p['policies'] for sf in m.SUFFIXES),
        max_net_delta=max(attribution[name][pol][sf]['products'][s]['net_delta'] for pol in p['policies'] for sf in m.SUFFIXES),
        min_known_net=min(results[pol][name][sf]['products'][s]['net'] for pol in p['policies'] for sf in m.SUFFIXES)) for s in r.SYMS} for name in results['original']}
    write('product-stress.json',product_stress)
    write('opportunity-ledgers.json',ledgers)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for name in results['original']:
            for z in accepted['original',name,m.PRIMARY]:
                f.write(json.dumps(dict(variant=name,row=z),ensure_ascii=False)+'\n')
    with (RUN/'rejected-diagnostic.jsonl').open('x',encoding='utf-8') as f:
        for z in diagnostic_rows:f.write(json.dumps(z,ensure_ascii=False)+'\n')
    write('sources.json',sources)
    write('checks.json',dict(qa=dict(qa),portfolio_controls=checks,
        earlier_contract_equality=None if only_2026 else m.old.read(SOURCE/'older-acquisition/source-equality.json'),
        production_changed_by_research=False,
        production_hash_differences_since_start=[k for k,h in p['production_before'].items() if m.old.sha(k)!=h]))
    chart(results)
    report(results,diagnostics,attribution,stress)
    write('completion.json',dict(files={q.name:m.old.sha(q) for q in RUN.iterdir() if q.is_file()},
        script_sha256=m.old.sha(__file__),research_script_sha256=m.old.sha(r.__file__),production_changed=False))
    print(json.dumps(dict(primary={n:dict(products=x[m.PRIMARY]['products'],focus=x[m.PRIMARY]['focus']) for n,x in results['original'].items()},rejected=diagnostics[m.PRIMARY],stress=stress)),flush=True)


def chart(results):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','SimHei','DejaVu Sans'],'axes.unicode_minus':False})
    names=list(results['original']);colors=['#64748b','#2563eb','#0d9488','#f59e0b','#8b5cf6','#dc2626']
    fig,axes=plt.subplots(2,1,figsize=(12,8),layout='constrained')
    x=np.arange(4);width=.12
    for i,name in enumerate(names):
        pro=results['original'][name][m.PRIMARY]['products']
        axes[0].bar(x+(i-2.5)*width,[pro[s]['n'] for s in r.SYMS],width,label=LABELS[name],color=colors[i])
        axes[1].bar(x+(i-2.5)*width,[pro[s]['net'] for s in r.SYMS],width,color=colors[i])
    for ax in axes:
        ax.set_xticks(x,[m.NAMES[s] for s in r.SYMS]);ax.grid(axis='y',alpha=.2);ax.axhline(0,color='#64748b',lw=.8)
    axes[0].set_ylabel('已平仓笔数');axes[1].set_ylabel('单手已知净额 / 元')
    axes[0].legend(ncol=2,fontsize=9,frameon=False)
    fig.suptitle('四品种放松规则：相同参数、全品种重新竞争\n'+SPAN+' · 主场景',fontsize=14)
    fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)


def report(results,diagnostics,attribution,stress):
    primary=results['original'];sf=m.PRIMARY
    lines=['# 四品种过滤机会与放松规则回放','',
        '新增回放范围：'+SPAN+'，行情截止2026-10-23 11:05；沿用原有实际覆盖，7月及10月不完整。主场景为每边1 tick、修正触价、单手、原未知占用规则。所有金额为扣冻结模型手续费后的已知平仓净额，回撤为已实现回撤，未含持仓浮亏、保证金和账户收益率。',
        '', '本批五组放松参数在结果产生前写入protocol.json；四个品种使用相同阈值。所有历史均已被反复研究，本批属于探索性回放，不是独立样本外验证。线上配置及冻结基线未修改。',
        '', '## 全品种交易数','', '本表沿用2025年9月—2026年10月23日的完整原始统计，与下文2026年新增回放的时段不同。全品种B5控制；限定六品种重新竞争时，PVC曾为81笔，此处是80笔，不能混用。', '', '|品种|B0|B2|B5|B5净额/元|','|---|---:|---:|---:|---:|']
    counts=m.old.read(RUN/'all-product-counts.json')
    for s,v in sorted(counts['B5'].items(),key=lambda kv:(-kv[1]['n'],kv[0])):
        lines.append(f"|{m.NAMES[s]} {s.rstrip('0')}|{counts['B0'][s]['n']}|{counts['B2'][s]['n']}|{v['n']}|{v['net']:,.2f}|")
    lines+=['','## 被挡住的机会主要盈利还是亏损','',
        '诊断D仅取消止损宽度／成本、目标成本比、A段净实体、计划及成交净盈亏比门槛，以原局部目标和原单手入场／退出规则逐候选回放。保留适应性确认、追价限制、节假日周末规则和数据检查。这里允许候选相互重叠，不能相加当成可执行组合收益；未触发、无效、无法确定的结果没有算成亏损或零收益。',
        '', '|品种|原被过滤候选|可确定平仓|净盈利|净亏损|净胜率|条件回放净额/元|','|---|---:|---:|---:|---:|---:|---:|']
    for s,d in diagnostics[sf].items():
        v=d['outcomes'];lines.append(f"|{m.NAMES[s]}|{d['total_rejected_candidates']}|{v['n']}|{v['wins']}|{v['losses']}|{v['win_rate']:.1%}|{v['net']:,.2f}|")
    lines+=['','逐道原过滤原因的数量和结果见rejected-outcomes.json；其余状态见diagnostic_statuses。盈利笔数占多数并不自动意味着总净额为正，盈亏幅度也决定结果。',
        '', '## 实际放松后，全品种重新竞争','',
        '只替换JD/SR/CJ/SI四品种的执行候选，其他18品种保持原B5。仍按触发时间竞争同品种槽位、板块同向限额及同A段两次原始止损限制。新增交易可能占掉原有交易，因此下面不是把被过滤盈利单直接加回原收益。',
        '', '|方案|四品种合计笔数|合计净额/元|较原净额变化|已实现回撤/元|未知机会|改善的压力场景|','|---|---:|---:|---:|---:|---:|---:|']
    control=primary['C_control'][sf]['focus']
    for name,cases in primary.items():
        v=cases[sf]['focus'];st=stress[name]
        lines.append(f"|{LABELS[name]}|{v['n']}|{v['net']:,.2f}|{v['net']-control['net']:+,.2f}|{v['realized_cash_drawdown']:,.2f}|{v['unknown']}|{st['positive_cases']}/{st['cases']}|")
    lines+=['','压力场景为两种分钟成交假设×每边1/2 tick×两种未知占用政策，共8组。正改善只代表相对本批B5，不能证明未来有效。',
        '', '## 分品种结果：笔数／净额','', '|方案|鸡蛋|白糖|红枣|工业硅|','|---|---:|---:|---:|---:|']
    for name,cases in primary.items():
        pro=cases[sf]['products'];lines.append('|'+LABELS[name]+'|'+'|'.join(f"{pro[s]['n']} / {pro[s]['net']:,.2f}" for s in r.SYMS)+'|')
    if EVALUATION_PERIODS==['early','recent']:
        lines+=['','## 当前判断','',
            '四品种被过滤且能确定平仓的条件回放，都以净亏损笔数为多数，合计净额也为负。过滤确实漏掉盈利机会，同时挡掉了更多亏损机会。不能把这些重叠的机会损益当可交易组合，也不能从胜率单独下结论。',
            '', '鸡蛋只把目标成本比10%放至20%，主场景9笔／389.83元变为11笔／837.83元，已实现回撤695.71降至479.64元；8组均比原规则改善，增益区间218.21—448.00元。但最差场景绝对净额仍为−1.89元，仅2笔主场景新增交易，不足以直接推广。',
            '', '取消局部目标截近的鸡蛋主场景更高（18笔／1,640.63元），但只有4/8组改善；压力下相对原规则可差228.24元。四品种合计8/8改善并不等于每个品种都8/8改善，必须分开看。',
            '', '白糖、工业硅的放松收益依成交假设变化，没有鸡蛋目标成本比方案同样一致；工业硅放松后仍为亏损。红枣单项放松基本不增加成交，多项一起放松则4笔／−224元变为14笔／−2,034元，不支持为了频率一并放宽。',
            '', '三项一起放松使四品种从41笔／−366.53元变为126笔／−3,054.78元，已实现回撤从1,845.74增到4,513.67元，8组全部变差。因此当前只保留鸡蛋目标成本比20%作为研究观察方案，不改线上、不把同一历史探索当独立验证。2025年补回放未完成，不能将此判断扩大为14个月结论。']
    period_labels={'older':'2025年9—12月四品种净额','early':'2026年1—6月','recent':'2026年近期段'}
    lines+=['','## 跨时段与增减交易','', '|方案|'+'|'.join(period_labels[q] for q in EVALUATION_PERIODS)+'|新增已平仓笔数/净额|被替代已平仓笔数/净额|共有交易盈亏变化|','|---|'+'---:|'*(len(EVALUATION_PERIODS)+3)]
    for name,cases in primary.items():
        v=cases[sf];d=attribution[name]['original'][sf]['focus']
        lines.append('|'+LABELS[name]+'|'+'|'.join(f"{v['periods'][q]['net']:,.2f}" for q in EVALUATION_PERIODS)+f"|{d['added']['n']} / {d['added']['net']:,.2f}|{d['removed']['n']} / {d['removed']['net']:,.2f}|{d['common_trade_net_change']:,.2f}|")
    lines+=['','完整逐月盈亏、各压力场景、四品种及全组合回撤见results.json。逐笔新增、减少和共有交易变化见attribution.json。',
        '', '## 校验与证据','',
        f"- 四品种原规则{m.old.read(RUN/'checks.json')['qa']['exact_frozen_control_rows']:,}条个别执行结果与冻结档案逐字段完全一致；全部8组全品种B5组合控制复现。",
        '- 2025年新回放尚未完成，天勤标准初始化连续超时；没有将2026年比较结果冒充14个月结果。' if EVALUATION_PERIODS==['early','recent'] else '- 2025年补取分钟数据按实际合约逐一比对原始规范化哈希；原始分钟仅存内存，不另存价格档案。',
        '- 被过滤机会的无门槛回放保留原已平仓交易的价格路径；手续费只扣一次，检查成交时间及tick网格。',
        '- 未知机会不计零盈亏，另做原保留至数据尾部和双路径均平仓可释放两种政策。',
        '', '![交易笔数与净额对照](comparison.png)', '',
        '参数：[protocol.json](protocol.json)；校验：[checks.json](checks.json)；逐笔组合：[accepted.jsonl](accepted.jsonl)；被挡机会：[rejected-diagnostic.jsonl](rejected-diagnostic.jsonl)。']
    (RUN/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':run(only_2026='--2026-only' in sys.argv)
