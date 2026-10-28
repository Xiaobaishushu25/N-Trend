"""Report saved candidate and independent stress/MTM/matched-clock audits."""
import collections
import json
import subprocess
import research_swing_jd_reclaim_robustness as r

RUN=r.RUN
R=r.R
T={'E20_ONLY':'E20S1完整对照','FULL':'冻结合并版／新增模块双向','CAP400':'新增模块计划风险≤400元','CAP800':'新增模块计划风险≤800元','LONG_ONLY':'新增模块仅做多','SHORT_ONLY':'新增模块仅做空'}
def f(x):return '—' if x is None else f'{x:,.2f}'

def run():
    p=R(RUN/'protocol.json');main=R(RUN/'main-results.json');july=R(RUN/'july-results.json');checks=R(RUN/'verification.json');primary=p['primary'];cases=sorted({k.rsplit('/',1)[0] for k in main});mtm=R(RUN/'mtm/results.json');null=R(RUN/'matched-null/results.json');con=R(RUN/'concentration.json');strata=R(RUN/'new-module-strata.json')
    frozen_manifest=R(r.SNAP/'manifest.json')
    for q,h in frozen_manifest['files'].items():assert r.sha(r.SNAP/q)==h,q
    assert r.sha(r.SNAP/'manifest.json')==p['snapshot_manifest_sha256']
    assert p['script_sha256']==r.sha(r.__file__)
    for q,h in R(RUN/'implementation-manifest.json').items():assert r.sha(RUN/'fork'/q)==h
    assert all(r.sha(r.b.ROOT/q)==h for q,h in p['production_before'].items())
    for q,h in R(RUN/'mtm/sources.json').items():assert r.sha(q)==h
    for paths in R(RUN/'matched-null/sources.json').values():
        for q,h in paths.items():assert r.sha(q)==h
    assert R(RUN/'mtm/protocol.json')['script_sha256']==r.sha(r.b.ROOT/'scripts/audit_swing_jd_reclaim_mtm.py')
    assert R(RUN/'matched-null/protocol.json')['script_sha256']==r.sha(r.b.ROOT/'scripts/audit_swing_jd_reclaim_matched_null.py')
    summary={}
    for m in r.MODELS:
        q=main[primary+'/'+m];xs=[main[case+'/'+m] for case in cases];summary[m]=dict(primary=q,min16cash=min(v['net'] for v in xs),min16normalized=min(v['normalized_net'] for v in xs),max16unknown=max(v['unknown'] for v in xs),per_slip={str(s):dict(min_cash=min(main[case+'/'+m]['net'] for case in cases if case.endswith('slip'+str(s))),min_normalized=min(main[case+'/'+m]['normalized_net'] for case in cases if case.endswith('slip'+str(s)))) for s in p['slips']})
    accepted=collections.defaultdict(list)
    for line in (RUN/'accepted-main-primary.jsonl').open(encoding='utf-8'):
        x=json.loads(line);accepted[x['variant']].append(x['row'])
    capovershoots={}
    for variant,budget in [('CAP400',400),('CAP800',800)]:
        entered=[v for v in accepted[variant] if v['module']=='S0_RECLAIM_R2' and r.f.EV(v).get('fill') is not None]
        capovershoots[variant]=dict(reclaim_entered=len(entered),actual_risk_above_planned_cap=sum(r.f.EV(v)['risk']*10>budget+1e-7 for v in entered),max_actual_cash_risk=max([0]+[r.f.EV(v)['risk']*10 for v in entered]))
    # Identify howmuch ofactualnew-moduleacceptedprofit is covered bystrictnull.
    statuses={v['key']:v['status'] for v in R(RUN/'matched-null/signals-and-matches.json')};matched_attribution={}
    for status in ['matched','insufficient_hosts','signal_forward_censored','signal_prefix_unverified']:
        rows=[v for v in accepted['FULL'] if v['module']=='S0_RECLAIM_R2' and statuses.get(v['original_event_key'])==status];matched_attribution[status]=r.b.z.metric(rows)
    r.write('comparison-summary.json',dict(models=summary,cap_actual_risk_diagnostic=capovershoots,matched_null_accepted_strata=matched_attribution))
    lines=['# 鸡蛋ABC收回候选：冻结保存与稳健性验证','',
        '2026-10-26。已先保存候选，再展开本轮检查。主候选的信号、计划2R目标、真实假突破极值止损、原E20S1参考及连续占仓规则均未改变；生产代码未改。','',
        '## 冻结保存','',
        f"当前快照：[jd-abc-reclaim-observation-v2](../../research-strategies/jd-abc-reclaim-observation-v2/README.md)。包含{len(frozen_manifest['files'])}个带SHA-256的文件、完整原研究证据、全部个体决策、研究源码、执行二进制及Rust源码、冻结手续费。",
        '运行快照目录内`python replay.py`，只用Python标准库即可从个体决策重新计算240项场景及时间块统计；不依赖行情服务或当前仓库脚本，入口禁止写入字节码缓存。分钟行情重新生成仍引用external-sources.json中的外部归档。v2仅是保存入口修订，候选规则与此前一致。','',
        '本轮另存于当前研究目录；不覆盖快照和此前盈利结果。原始主场景仍为41笔已平仓净额4,478.255元、1笔未平仓。','',
        '## 扩大执行压力','',
        '把每次成交的不利滑点从1、2tick扩展到3、4tick，保留正常与额外一分钟延迟、两种未知占仓政策，共16种场景。逐笔实际分钟成交、净盈亏比复核、追价拒绝、止损止盈及占仓重新运行；不是从旧利润中机械扣滑点。计划经济性与其滑点假设保留原定义。',
        f"原1、2tick的{checks['exact_prior_individual_rows']}条个体结果与冻结证据完全一致，完整合并版和E20对照的{checks['exact_prior_portfolio_cases']}项原组合统计也完全一致。",'',
        '|组合|主场景平仓数|主净额|16场景最低净额|最低统一风险净额|最多未知|','|---|---:|---:|---:|---:|---:|']
    for m,q in summary.items():lines.append(f"|{T[m]}|{q['primary']['n']}|{f(q['primary']['net'])}|{f(q['min16cash'])}|{f(q['min16normalized'])}|{q['max16unknown']}|")
    lines+=['','金额均为一手扣冻结手续费及滑点后的已平仓元；统一风险为每笔计划风险300元的研究归一，非账户收益或可执行的小数手。未知收益不按0元补齐。','',
        '|组合|滑点1最低净额|滑点2|滑点3|滑点4|','|---|---:|---:|---:|---:|']
    for m,q in summary.items():lines.append('|'+T[m]+'|'+'|'.join(f(q['per_slip'][str(s)]['min_cash']) for s in p['slips'])+'|')
    lines+=['','完整合并版的一手已平仓净额在16场景均为正，但统一风险最差约−35元，未达到全部口径均为正。E20对照在扩大压力后也可转亏。更多滑点可能改变是否成交、净盈亏比和后续占仓，所以不同滑点结果不保证单调。','',
        '## 止损宽度和方向依赖','',
        '以下筛选只作用于新增收回模块；E20对照始终保留完整双向规则。400/800元是预警时已知的一手计划风险诊断阈值，不是账户预算建议。筛选发生在下单前，原始订单重新竞争，不按实际盈亏或未来成交价过滤。','',
        '|完整合并版中已平仓的新增模块分组|笔数|净额|统一风险净额|','|---|---:|---:|---:|']
    for label,zh in [('long','做多'),('short','做空'),('risk_le400','计划风险≤400'),('risk_400_800','400＜计划风险≤800'),('risk_gt800','计划风险＞800')]:
        q=strata[label];lines.append(f"|{zh}|{q['n']}|{f(q['net'])}|{f(q['normalized_net'])}|")
    lines+=['','做多12笔贡献约2,186元，做空10笔亏约466元。仅保留新增做多的组合在16场景现金和统一风险收益均为正，但平仓数降为31笔，仍有未平仓，且同一历史已被反复使用，不能据此直接升级为只做多。',
        '盈利也并非主要来自窄止损：计划风险400—800元的8笔贡献约2,044元，而≤400元的13笔合计亏约712元。硬压到400元后，新增占仓变化虽让主场景有36笔，收益和压力表现明显变差。该差异还可能包含波动环境与行情阶段影响，不能解释为扩大止损就会赚钱。',
        '800元计划风险限制的组合主场景42笔、净额3,632元，未知为0；但16场景最低统一风险仍亏约668元，10月主场景转亏。上限不能自动改善所有风险指标。实际入场跳空及滑点还可能让实际风险超过计划上限，详情见comparison-summary.json。','',
        '|组合|Q1|Q2|Q3覆盖段|10月部分|','|---|---:|---:|---:|---:|']
    for m,q in summary.items():lines.append('|'+T[m]+'|'+'|'.join(f(q['primary']['blocks'][k]['net']) for k in ['Q1_train','Q2','Q3','October_partial'])+'|')
    lines+=['','## 利润集中度','',
        '按同一父A结构整体归组，避免把同一结构中的多次交易当成独立利润；另按预警所在周归组。删除赢家仅作结果敏感性诊断，没有删除原订单再读入其他交易，也不是新的交易规则。','',
        '|删除单位|删最佳1组后现金净额|删最佳3组后现金净额|删最佳3组后统一风险净额|','|---|---:|---:|---:|']
    for name,zh in [('Acluster','父A结构'),('week','周')]:q=con[name];lines.append(f"|{zh}|{f(q['ex_best_cash'])}|{f(q['ex_top3_cash'])}|{f(q['ex_top3_normalized'])}|")
    lines+=['','完整合并版删除最佳3个A结构或最佳3周后，已知现金及统一风险净额仍为正，支持其利润来源比单独收回模块更分散；这仍是旧历史内的尾部敏感性检查。','',
        '## 一分钟浮动权益风险','',
        '使用冻结接受序列，在实际合约每个可用分钟收盘价标记权益，扣已付开仓费和假设平仓手续费、不利滑点；实际平仓时使用真实回放净额。入场前分钟极值不计入持仓风险。此为分钟收盘采样，不能代表完整tick路径或保证金强平结果。','',
        '|组合|仅平仓统一风险回撤|分钟浮动统一风险回撤|分钟浮动一手现金回撤|','|---|---:|---:|---:|']
    for key,zh in [('E20S1','E20S1对照'),('FULL','冻结完整合并版')]:q=mtm[key];lines.append(f"|{zh}|{f(q['closed_only_normalized_dd'])}|{f(q['minute_close_normalized_dd'])}|{f(q['minute_close_cash_dd'])}|")
    opened=mtm['FULL']['open_position'];lines+=['',f"最后未平仓头寸按{opened['last_quote_ts']}真实收盘{opened['last_quote_close']}标记，扣假设退出成本后浮盈约{f(opened['marked_floating_after_costs'])}元。它没有加入4,478元已平仓净额，也未被当作已平仓盈利。",'浮动回撤表明此前1,262元的统一风险平仓回撤低估了持仓过程的起伏；分钟收盘浮动回撤约1,636元，风险不能只看已平仓曲线。','',
        '## 匹配时点检验','',
        '使用全部32个主历史收回决策，不按是否成交或盈利筛选。在同一实际合约、月份、15分钟时段、过去8柱涨跌方向及相近ATR范围中找对照，排除真实信号前后16柱附近。固定条件至少3个可用对照，无事后放宽。199次有固定种子的抽取，每次对照时点不重复。',
        f"严格条件只匹配到{null['matched']}个信号，16个对照不足、1个后续路径不完整。匹配子集未来16柱平均方向推进{f(null['actual']['mean_close_ATR'])}ATR，对照中位数{f(null['null']['mean_close_ATR']['median'])}ATR，约处于对照第{f(null['null']['mean_close_ATR']['actual_percentile'])}百分位。先+1ATR为6次、先−1ATR为9次。",'目前没有显示出明显超越这些相近时点的额外方向优势。样本少、窗口仍相关，这不是显著性检验，也不能断言结构完全无效；检验只看价格路径，不含真实止损、目标、成本与占仓。组合收益改善可能同时来自风险几何、退出和机会选择，不能全归因于预警具有强预测力。','',
        '## 保存后的研究判断','',
        '1. 冻结原候选继续保留：扩大滑点后现金收益仍有余量，按A结构/周删除头部利润后仍为正。',
        '2. 新增模块对方向及止损宽度有明显依赖；只做多和风险上限均作为诊断分支保存，不覆盖完整候选，也不据此继续扫阈值。',
        '3. 浮动回撤比平仓回撤更大，严格匹配时点检验尚未证明额外方向优势。后续优先冻结规则后补新时段，并同时记录全部失守、未收回、收回但经济性拒绝、成交和未平仓，检验是否继续优于完整E20对照。',
        '4. 目前仍未满足至少60笔与独立泛化验证要求。EG/PVC及生产规则未改，未新增实盘或定时交易任务。','',
        '证据：[冻结协议](protocol.json) · [16场景](main-results.json) · [因果核验](verification.json) · [集中度](concentration.json) · [浮动权益](mtm/results.json) · [匹配时点](matched-null/results.json) · [完成核验](completion.json)。','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8');plot(summary,mtm)
    for q in main.values():assert abs(sum(q['month_cash'].values())-q['net'])<1e-6
    nativehash={}
    for per in ['early','recent','july']:
        for model in p['source_models']:
            path=RUN/per/model/'executions/evaluations.jsonl';nativehash[path.relative_to(RUN).as_posix()]=r.sha(path)
    r.write('completion.json',dict(completed=True,production_changed=False,frozen_candidate_unchanged=True,untouched_OOS=False,
        snapshot_manifest_sha256=r.sha(r.SNAP/'manifest.json'),checks=checks,native_hashes=nativehash,report_sha256=r.sha(RUN/'summary.md'),report_source_sha256=r.sha(__file__),
        artifacts={q.relative_to(RUN).as_posix():r.sha(q) for q in RUN.rglob('*.json') if 'fork' not in q.parts and q.name!='completion.json'}))
    print(json.dumps(dict(snapshot_unchanged=True,main={m:dict(n=q['primary']['n'],net=q['primary']['net'],min16=q['min16cash']) for m,q in summary.items()},mtm_normalized_dd=mtm['FULL']['minute_close_normalized_dd'],null_matched=null['matched']),ensure_ascii=False))

def plot(summary,mtm):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams['font.sans-serif']=['Microsoft YaHei'];plt.rcParams['axes.unicode_minus']=False
    fig,axes=plt.subplots(1,2,figsize=(15,6),gridspec_kw={'width_ratios':[1.15,1]})
    values=np.array([[summary[m]['per_slip'][str(s)]['min_cash'] for s in range(1,5)] for m in r.MODELS]);limit=max(abs(values.min()),abs(values.max()));im=axes[0].imshow(values,cmap='RdYlGn',vmin=-limit,vmax=limit,aspect='auto')
    axes[0].set_yticks(range(len(r.MODELS)),[T[m] for m in r.MODELS],fontsize=9);axes[0].set_xticks(range(4),[f'{s} tick/次成交' for s in range(1,5)]);axes[0].set_title('每档滑点下最差场景：一手已平仓净额')
    for i in range(len(r.MODELS)):
        for j in range(4):axes[0].text(j,i,f'{values[i,j]:.0f}',ha='center',va='center',fontsize=10)
    months=list(summary['FULL']['primary']['month_cash'])
    for m in ['E20_ONLY','FULL','LONG_ONLY']:
        vals=summary[m]['primary']['month_cash'];total=0;ys=[]
        for mo in months:total+=vals[mo];ys.append(total)
        axes[1].plot(months,ys,marker='o',ms=3,label=T[m])
    axes[1].axhline(0,c='#999999',lw=.8);axes[1].tick_params(axis='x',rotation=55);axes[1].set_title('主场景累计已平仓净额；未知另计');axes[1].legend(fontsize=8);axes[1].set_ylabel('元');fig.tight_layout();fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)

if __name__=='__main__':run()
