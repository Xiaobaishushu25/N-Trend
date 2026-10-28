"""Failure reversal, sequential reclaim and continuously competed overlay report."""
import collections
import copy
import json
import statistics as st
import research_swing_jd_reclaim_overlay as o

c=o.c
f=c.f
b=f.b
R=f.R
RUN=f.RUN
def title(m):
    if m in f.CONTROLS:return {'E20S1':'原S1＋成本20%','TOUCH_BTRUE':'原触发＋整个B止损','C15_BTRUE':'原C15推进后回踩'}[m]
    if m==f.PLACEBO:return 'A起点失守／原方向诊断对照'
    if m in o.MODELS:return '原策略＋收回A起点／'+('原S1' if m.endswith('S1') else '计划2R')
    l,e,g=m.split('_');return ('A起点失守' if l=='S0' else '局部B破位')+'／'+{'BREAK':'直接反向','RETEST':'回抽失败反向','RECLAIM':'收回后原方向'}[e]+'／'+{'A1':'A等幅目标','R2':'计划2R','S1':'原S1'}[g]
def fmt(v):return '—' if v is None else f'{v:,.2f}'

def strict_overlay_selection(p):
    ctl,meta,data,ends,_=c.load();accepted={}
    for case in f.cases(p):
        pol,sf=case.split('/',1)
        for m,target in zip(o.MODELS,['S1','R2']):
            stream=[];mm=dict(meta)
            for prefix,source in [('0','E20S1'),('1','S0_RECLAIM_'+target)]:
                for r0 in data[source,sf]:
                    r=copy.deepcopy(r0);key=prefix+'|'+r['event_key'];mm[key]=meta[r['event_key']];r['event_key']=key;stream.append(r)
            accepted[case,m]=b.admit(stream,ends,pol,ctl,mm)[0]
    dates=R(b.RUN/'generation-checks.json')['covered_dates'];selection=[]
    for cutoff,end in b.FOLDS:
        days=sum(d<cutoff[:10] for d in dates);eligible=[];why={};metrics={}
        base={case:b.z.metric(b.z.x.visible(b.admit(data['E20S1',case.split('/',1)[1]],ends,case.split('/',1)[0],ctl,meta)[0],cutoff)) for case in f.cases(p)}
        for m in o.MODELS:
            xs={case:b.z.metric(b.z.x.visible(accepted[case,m],cutoff)) for case in f.cases(p)};q=xs[p['primary']];reasons=[];metrics[m]=xs
            if q['n']<20 or q['n']/days<.35:reasons.append('train_frequency')
            if min(x['normalized_net'] for x in xs.values())<=0:reasons.append('stress_nonpositive')
            if q['normalized_ex_best']<=0:reasons.append('ex_best_nonpositive')
            if any(x['normalized_net']<base[case]['normalized_net']-1e-7 for case,x in xs.items()):reasons.append('below_reference')
            if reasons:why[m]=reasons
            else:eligible.append((min(x['normalized_net'] for x in xs.values())-.25*max(x['normalized_realized_dd'] for x in xs.values()),m))
        eligible.sort(key=lambda q:(-round(q[0],8),o.MODELS.index(q[1])));selection.append(dict(start=cutoff,end=end,chosen=eligible[0][1] if eligible else 'E20S1',rejections=why,visible_metrics=metrics))
    return selection,accepted

def verify(p,cp):
    amendment=R(RUN/'analysis-amendment.json');assert f.sha(RUN/'initial-source.py')==p['script_sha256']==amendment['initial_source_sha256'];assert f.sha(f.__file__)==amendment['current_source_sha256']
    assert all(f.sha(b.ROOT/q)==h for q,h in p['production_before'].items());assert all(f.sha(q)==h for q,h in p['dependencies'].items())
    assert c.sha(c.__file__)==cp['script_sha256'];assert f.sha(p['native_binary']['path'])==p['native_binary']['sha256']
    dep=R(c.RUN/'dependency-amendment.json');assert dep['parent_current_source_sha256']==f.sha(f.__file__);assert dep['parent_analysis_amendment_sha256']==f.sha(RUN/'analysis-amendment.json')
    for q,h in cp['sources'].items():assert (f.sha(RUN/'initial-source.py') if q==str(b.ROOT/'scripts/research_swing_jd_abc_failure_reversal.py') else f.sha(q))==h
    overlayp=R(o.RUN/'protocol.json');assert overlayp['script_sha256']==f.sha(o.__file__);assert overlayp['source_results_sha256']==f.sha(c.RUN/'main-results.json')
    sourcehash={};n=closed=0
    for root,models in [(RUN,f.NEW+[f.PLACEBO]),(c.RUN,c.NEW)]:
        for per in ['early','recent','july']:
            for m in models+(['TOUCH_BTRUE','E20S1'] if root==RUN and per=='july' else []):
                path=root/per/m/'executions/evaluations.jsonl';sourcehash[str(path.relative_to(RUN))]=f.sha(path)
                for line in path.open(encoding='utf-8'):
                    row=json.loads(line);n+=1
                    for fill in row['result']['fills']:
                        assert fill['ts']>row['warning_ts'];assert abs(fill['price']/row['tick_size']-round(fill['price']/row['tick_size']))<1e-6
                    if f.EV(row)['status']=='closed':
                        closed+=1;e=f.EV(row);sign=f.direction(row);assert e['exit_ts']>=e['entry_ts']>row['warning_ts'];assert abs((e['fill']-row['stop'])*sign-e['risk'])<1e-6;assert e['risk']>=10-1e-6
            if root==RUN:
                for q,h in R(root/per/'generation-checks.json')['source_hashes'].items():assert f.sha(q)==h
    # No first activation was chosen by native trade status or entry economics.
    clusters=collections.Counter()
    for per in ['early','recent','july']:
        allfail=collections.defaultdict(list)
        for q in R(RUN/per/'failure-ledger.json'):
            if q['reason']=='failure_confirmed':allfail[q['level_name'],q['cluster']].append((q['failure_ts'],q['seed_ts'],q['parent_key']))
        for q in R(RUN/per/'first-activations.json'):
            key=q['level_name'],q['cluster'];assert (q['failure_ts'],q['seed_ts'],q['parent_key'])==min(allfail[key]);clusters[key]+=1
    assert max(clusters.values())==1
    return dict(native_rows=n,closed_native_rows=closed,strict_entry_after_decision=True,planned_actual_risk_checked=True,price_grid_checked=True,
        first_activation_before_economics_verified=True,one_activation_per_A_across_windows=True,production_unchanged=True,python_tests_passed=12,original_control_native_rows_verified=R(RUN/'main-verification.json')['exact_native_controls']),sourcehash

def run():
    p=R(RUN/'protocol.json');cp=R(c.RUN/'protocol.json');primary=p['primary'];cases=f.cases(p);main=R(RUN/'main-results.json');follow=R(c.RUN/'main-results.json');overlay=R(o.RUN/'main-results.json')
    verification,sourcehash=verify(p,cp);summary={};passed=[]
    model_sources=[(m,main) for m in f.MODELS]+[(m,follow) for m in c.NEW]+[(m,overlay) for m in o.MODELS]
    for m,source in model_sources:
        q=source[primary+'/'+m];xs=[source[case+'/'+m] for case in cases]
        flags=dict(n60=q['n']>=60,cash_positive_all8=min(x['net'] for x in xs)>0,normalized_positive_all8=min(x['normalized_net'] for x in xs)>0,
            ex_top3_positive=q['normalized_ex_top3']>0,three_positive_blocks=sum(x['net']>0 for x in q['blocks'].values())>=3,no_unknown=max(x['unknown'] for x in xs)==0)
        summary[m]=dict(primary=q,min8cash=min(x['net'] for x in xs),min8normalized=min(x['normalized_net'] for x in xs),max8unknown=max(x['unknown'] for x in xs),flags=flags)
        if m not in f.CONTROLS+[f.PLACEBO] and all(flags.values()):passed.append(m)
    pathsummary={}
    for level in f.LEVELS:
        xs=[q for q in R(RUN/'failure-paths.json') if q['failure_name']==level and q['complete']]
        pathsummary[level]=dict(n=len(xs),mean_close_ATR=st.mean(q['end_r'] for q in xs),mean_MFE_ATR=st.mean(q['mfe'] for q in xs),mean_MAE_ATR=st.mean(q['mae'] for q in xs),race=dict(collections.Counter(q['race'] for q in xs)))
    ctl,meta,data,ends,_=f.load();sf=primary.split('/',1)[1];reverse={r['event_key']:r for r in data['S0_BREAK_R2',sf]};original={r['event_key']:r for r in data[f.PLACEBO,sf]};shared=[k for k in reverse.keys()&original.keys() if f.EV(reverse[k])['status']==f.EV(original[k])['status']=='closed']
    paired=dict(both_closed=len(shared),reverse_net=sum(f.EV(reverse[k])['net_amount'] for k in shared),original_net=sum(f.EV(original[k])['net_amount'] for k in shared),before_portfolio_occupancy=True)
    selection,accepted=strict_overlay_selection(p);f.write('overlay-cutoff-diagnostic.json',selection)
    # Conditional stop-close scenario for actual unfinished fills, never a real
    # execution, never a bound on gap losses and never inserted into base PnL.
    fees=R(b.ROOT/'scripts/exchange_fees_20261022.json')['products']['JD'];stress={}
    charge=lambda leg,price:fees[leg]['fixed_yuan_per_lot']+fees[leg]['notional_rate']*price*10
    for case in cases:
        for m in o.MODELS:
            unknown=[r for r in accepted[case,m] if f.EV(r)['status']=='unfinished' and f.EV(r).get('fill') is not None];extra=extra_norm=0
            for r in unknown:
                sign=f.direction(r);fill=f.EV(r)['fill'];exitprice=r['stop']-sign*r['slip']*r['tick_size'];net=(exitprice-fill)*sign*10-charge('open',fill)-max(charge('close_today',exitprice),charge('close_yesterday',exitprice));extra+=net;extra_norm+=net*300/(r['planned_risk']*10)
            base=overlay[case+'/'+m];stress[case+'/'+m]=dict(unfinished_count=len(unknown),hypothetical_extra_cash=extra,closed_plus_stop_scenario_cash=base['net']+extra,closed_plus_stop_scenario_normalized=base['normalized_net']+extra_norm)
    f.write('comparison-summary.json',dict(models=summary,screen_pass=passed,paths=pathsummary,paired_direction_diagnostic=paired,stop_scenario=stress))
    # Preserve complete funnels; partial data and filtered plans do not become0.
    funnel=collections.defaultdict(collections.Counter)
    for q in sourcehash:
        parts=q.replace('\\','/').split('/');period=next(z for z in parts if z in ['early','recent','july']);model=parts[parts.index(period)+1]
        for line in (RUN/q).open(encoding='utf-8'):
            r=json.loads(line)
            if r['scenario'].split('/',1)[1]==sf:funnel[period+'/'+model][f.EV(r)['status']]+=1
    f.write('funnel.json',{k:dict(v) for k,v in funnel.items()})
    lines=['# 鸡蛋ABC：延续失败、反向扩展与收回关键位','',
        '2026-10-26。本轮先冻结8个反向候选及1个同钟方向对照；看到方向对照结果后，追加4个“收回关键位”方案，再验证2个原策略与新模块合并方案。各阶段分别保存协议、源哈希和逐笔结果。后两阶段明确是顺序探索。','',
        'ABC继续作为结构背景。EG/PVC完整B5及生产代码均未修改。主历史为2026年1—6月、7月1—10日、8月10日—10月23日11:05；7月13—24日固定JD2608窗口单列复核。旧历史已研究多次，无未触碰样本外或泛化性保证。','',
        '## 失效定义与真实下单','',
        '- A起点失守：ABC原S0被一根完成15分钟柱收盘越过至少1tick；原B期间已收盘失守S0者排除。局部B破位只作为较弱诊断，不等于整个ABC失效。',
        '- 原ABC整段须属于同一实际合约。从原预警后监测最多60根完成15分钟柱；先碰原S1、出现缺失数据、换月或超过评分窗口则取消。',
        '- 每个A/失效定义仅取首次失效，先确定身份再检查回抽、费用、成交或盈利。同A重复预警不增加独立机会，也不挑旧亏损单反手。',
        '- 反向方案分别测试失效后下一分钟开盘，以及12柱内回抽关键位后再向反向拒绝；止损取决策时最近3柱逆交易方向极值外原缓冲，目标为失效位反向一倍A幅度或计划2R。',
        '- 同钟原方向对照使用相同失效时点和计划风险距离、对称人工止损及计划2R，专门检查方向；它没有真实止损锚点，不是可直接采用的策略。','',
        '所有方案保留计划与成交净盈亏比、费用空间、最小风险、追价、节假日、最长持有和连续占仓规则。金额是一手扣冻结手续费及滑点后的元；统一风险值是假设每笔计划风险300元，不是账户收益。8场景是执行与未知处理压力，不是8份独立行情。','',
        '## 第一阶段：反向扩展','',
        '|方案|已平仓数|已平仓净额|8场景最低净额|归一净额|最低归一|最多未知|','|---|---:|---:|---:|---:|---:|---:|']
    def addtable(models):
        for m in models:
            q=summary[m];v=q['primary'];lines.append(f"|{title(m)}|{v['n']}|{fmt(v['net'])}|{fmt(q['min8cash'])}|{fmt(v['normalized_net'])}|{fmt(q['min8normalized'])}|{q['max8unknown']}|")
    addtable(f.NEW+[f.PLACEBO])
    lines+=['',f"46个可下单前评估的A起点失守时点中，随后16根完整15分钟柱沿反向的平均收盘推进为{fmt(pathsummary['S0']['mean_close_ATR'])}ATR；局部B破位组为{fmt(pathsummary['BTRUE']['mean_close_ATR'])}ATR。不是所有失守都继续向外扩展。",
        f"在同钟、同计划风险、双向均已独立平仓的{paired['both_closed']}个事件中，反向净额{fmt(paired['reverse_net'])}元，原方向净额{fmt(paired['original_net'])}元。该比较先于占仓竞争，减少单纯接受名单差异；仍是条件历史诊断，不证明ABC优于一般均值回归。",
        '原方向对照的1笔未知是10月22日开仓后数据结束尚未平仓，不是按0元计入盈利。A等幅反向方案现金净额为负、统一风险净额却为正，说明一手与等风险表现不同；但去最佳3笔后归一仍转亏，不能只挑有利口径。','',
        '## 第二阶段：失守后重新收回','',
        '在已冻结的首次失效事件后12柱内，等待第一次完成收盘重新越过关键位至少1tick，沿原A方向入场。止损取从失效柱至收回柱的真实假突破极值外原缓冲，冻结不再扩大。先碰原S1或反向一倍A扩展、出现未核实柱则取消。目标分别为原S1和决策时计划2R。','',
        '|方案|已平仓数|已平仓净额|8场景最低净额|归一净额|最低归一|最多未知|','|---|---:|---:|---:|---:|---:|---:|']
    addtable(c.NEW)
    lines+=['','“收回A起点”优于仅收回局部B极值，但单独运行仍低频、利润集中。计划2R版25笔已平仓净额2,535元，删除最佳3笔后归一亏701元；原S1版17笔，压力下归一收益可转负。两者均未成为独立稳定策略。','',
        '## 第三阶段：接入原策略后重新竞争','',
        '合并的是全部原始订单决策，不是两张已接受交易表。仍一单/一仓，同A共享两次已实现止损限制；同钟先原策略，未知持仓继续占位。新规则改变后续占仓路径，必须计入被替代的原交易。','',
        '|方案|已平仓数|已平仓净额|8场景最低净额|归一净额|最低归一|最多未知|','|---|---:|---:|---:|---:|---:|---:|']
    addtable(['E20S1']+o.MODELS)
    lines+=['','|方案|Q1|Q2|Q3覆盖段|10月部分|删最佳3笔后归一|删最佳月份后归一|归一已实现回撤|','|---|---:|---:|---:|---:|---:|---:|---:|']
    for m in ['E20S1']+o.MODELS:
        q=summary[m]['primary'];lines.append('|'+title(m)+'|'+'|'.join(fmt(q['blocks'][k]['net']) for k in ['Q1_train','Q2','Q3','October_partial'])+'|'+fmt(q['normalized_ex_top3'])+'|'+fmt(q['normalized_ex_best_month'])+'|'+fmt(q['normalized_realized_dd'])+'|')
    attr=R(o.RUN/'incremental-attribution.json');a=attr[o.MODELS[1]]
    lines+=['',f"计划2R合并版保留原策略{a['kept_original_n']}笔，新增收回交易{a['new_reclaim_n']}笔贡献{fmt(a['new_reclaim_net'])}元，同时替代原策略{a['removed_original_n']}笔、其原净额为{fmt(a['removed_original_net'])}元，合计改善{fmt(a['total_net_delta'])}元。并非把独立2,535元直接加到原策略上。",
        '主场景由23笔增至41笔，四个时间块净额均为正，去最佳3笔后归一仍为正；收益分布比单独模块更分散。但归一已实现回撤由689元升至1,262元，5月仍贡献约62%的现金净额，仍有亏损月份。不能称为收益翻倍而风险不变。',
        '计划2R合并版还有1笔未平仓，未知收益未按0元填充。若仅作“假设按原止损外滑点平仓”的附加情景，各压力下已平仓加该假设平仓的最低现金净额为'+fmt(min(stress[case+'/'+o.MODELS[1]]['closed_plus_stop_scenario_cash'] for case in cases))+'元，最低归一为'+fmt(min(stress[case+'/'+o.MODELS[1]]['closed_plus_stop_scenario_normalized'] for case in cases))+'元。该情景不是已实现结果，也不是跳空损失上界。','',
        '## 时间迁移与已知7月窗口','',
        '采用此前固定的20笔训练、每覆盖日0.35笔、所有压力归一为正、去最佳笔后为正、逐场景不低于原E20S1等条件；每次仅读取截止日前严格平仓的结果。反向、追加收回与合并方案均没有在三个截止日获得采用资格，仍回退原研究对照。合并方案主要仍受训练频率限制。','']
    lines.extend('- '+q['start'][:7]+'：'+title(q['chosen']) for q in selection)
    lines+=['','7月13—24日是已知历史复核：收回A起点计划2R版1笔净赚726元，原S1版无成交；局部B收回两版各2笔合计亏228元。这段样本很小，不能当作重新获得的独立样本外。含该段的完整连续竞争结果另存，未将原C15无该段候选的数值当作同覆盖比较。','',
        '## 本轮判断','',
        '本轮找到一个更值得冻结观察的方向：原策略增加“真正失守A起点、随后重新收回”的补充机会。直接反向延续和局部B破位版本目前不支持采用。',
        '两个合并版本仍未满足至少60笔及完整未知处理要求；这轮旧历史启发了追加规则，也尚无新的独立验证。保留两个版本作研究候选，优先用未来新增数据检验收回模块的增量，避免继续在旧数据上调回收阈值、目标倍数或月份过滤。',
        'EG/PVC完整B5及生产代码保持原状。本轮不承诺鸡蛋达到EG一半收益，也不宣称完成泛化验证。','',
        '证据：[反向协议](protocol.json) · [反向全部场景](main-results.json) · [追加收回协议](reclaim-followup/protocol.json) · [收回全部场景](reclaim-followup/main-results.json) · [合并协议](reclaim-followup/overlay/protocol.json) · [合并全部场景](reclaim-followup/overlay/main-results.json) · [增量归因](reclaim-followup/overlay/incremental-attribution.json) · [完成核验](completion.json)。','',
        '核验说明：合并7月历史时补充了原始对照键的A聚类元数据，另存分析修订记录；未改信号、原生成结果或分钟成交。12项机制测试、首次失效因果前缀、整数tick、决策后成交、实际风险和原对照1760条均已核对。','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8');plot(summary)
    for _,source in model_sources:
        for q in source.values():assert abs(sum(q['month_cash'].values())-q['net'])<1e-6
    f.write('completion.json',dict(completed=True,production_changed=False,untouched_OOS=False,screen_pass=passed,verification=verification,native_hashes=sourcehash,
        report_sha256=f.sha(RUN/'summary.md'),report_source_sha256=f.sha(__file__),artifacts={str(q.relative_to(RUN)):f.sha(q) for q in RUN.rglob('*.json') if q.name!='completion.json'}))
    print(json.dumps(dict(passes=passed,verification=verification,overlay={m:dict(n=summary[m]['primary']['n'],net=summary[m]['primary']['net'],worst=summary[m]['min8cash']) for m in o.MODELS}),ensure_ascii=False))

def plot(summary):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif']=['Microsoft YaHei'];plt.rcParams['axes.unicode_minus']=False
    selected=['E20S1','S0_BREAK_R2',f.PLACEBO,'S0_RECLAIM_R2']+o.MODELS;fig,axes=plt.subplots(1,2,figsize=(15,6))
    for i,m in enumerate(selected[::-1]):
        q=summary[m];axes[0].plot([q['min8cash'],q['primary']['net']],[i,i],c='#a1b4c4');axes[0].scatter(q['primary']['net'],i,c='#208171' if q['primary']['net']>0 else '#b34f43',s=35)
    axes[0].set_yticks(range(len(selected)),[title(m) for m in selected[::-1]],fontsize=9);axes[0].axvline(0,c='#999999',lw=.8);axes[0].set_title('阶段结果：只列已平仓净额');axes[0].set_xlabel('元，线段左端为8场景最低值；未知另计')
    for m in ['E20S1']+o.MODELS:
        q=summary[m]['primary'];months=list(q['month_cash']);ys=[];total=0
        for mo in months:total+=q['month_cash'][mo];ys.append(total)
        axes[1].plot(months,ys,marker='o',ms=3,label=title(m))
    axes[1].axhline(0,c='#999999',lw=.8);axes[1].tick_params(axis='x',rotation=55);axes[1].set_title('连续占仓重竞争，按预警月份归属');axes[1].set_ylabel('一手累计已平仓净额（元）');axes[1].legend(fontsize=8)
    fig.tight_layout();fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)

if __name__=='__main__':run()
