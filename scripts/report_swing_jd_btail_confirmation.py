"""All B-tail variants, costs, concentration and causal generation checks."""
import collections
import json
import statistics as st
import research_swing_jd_btail_confirmation as t

RUN=t.RUN
R=t.R
F={'HOLD2':'连续两柱转向','PIVOT3':'三柱局部转折','BOS3':'突破前三柱区间','ANY':'三类最先确认'}
def title(m):
    if m in t.CONTROLS:return {'TOUCH_BTRUE':'原触发＋整个B止损','C15_BTRUE':'原C15推进后回踩确认','E20S1':'原S1＋成本20%'}[m]
    family,entry=m.split('_');return F[family]+'／'+('确认后开盘' if entry=='OPEN' else '确认柱极值外触发')
def f(v):return '—' if v is None else f'{v:,.2f}'

def run():
    p=R(RUN/'protocol.json');primary=p['primary'];cases=t.cases(p);main=R(RUN/'main-results.json');combined=R(RUN/'combined-results.json');july=R(RUN/'july-results.json')
    paths=R(RUN/'confirmation-paths.json');meta={};ledger=[];funnel=collections.defaultdict(collections.Counter);sourcehash={};rows=collections.defaultdict(dict)
    for period in ['early','recent','july']:
        meta.update(R(RUN/period/'metadata.json'))
        ledger.extend(dict(r,period=period) for r in R(RUN/period/'confirmation-ledger.json'))
        for m in t.NEW+(['TOUCH_BTRUE','E20S1'] if period=='july' else []):
            path=RUN/period/m/'executions/evaluations.jsonl';sourcehash[str(path.relative_to(RUN))]=t.sha(path)
            for line in path.open(encoding='utf-8'):
                r=json.loads(line)
                if r['scenario'].split('/',1)[1]==primary.split('/',1)[1]:funnel[period+'/'+m][t.EV(r)['status']]+=1
    for line in (RUN/'accepted-main-primary.jsonl').open(encoding='utf-8'):
        x=json.loads(line);r=x['row']
        if t.EV(r)['status']=='closed':
            key=meta[r['event_key']]['parent_key'] if x['model'] in t.NEW else r['event_key']
            rows[x['model']][key]=r
    summary={};passes=[]
    for m in t.MODELS:
        q=main[primary+'/'+m];xs=[main[c+'/'+m] for c in cases]
        flags=dict(n60=q['n']>=60,positive_all8=min(z['net'] for z in xs)>0,positive_normalized_all8=min(z['normalized_net'] for z in xs)>0,
            ex_best3_positive=q['normalized_ex_top3']>0,positive_3_blocks=sum(v['net']>0 for v in q['blocks'].values())>=3,no_unknown=max(z['unknown'] for z in xs)==0)
        summary[m]=dict(primary=q,min8net=min(z['net'] for z in xs),min8normalized=min(z['normalized_net'] for z in xs),max8unknown=max(z['unknown'] for z in xs),flags=flags)
        if m in t.NEW and all(flags.values()):passes.append(m)
    path_summary={};same_vs_later={};delay={}
    for fam in t.FAMILIES:
        xs=[r for r in paths if r['family']==fam and r['complete']]
        path_summary[fam]=dict(n=len(xs),mean_close_ATR=st.mean(r['end_r'] for r in xs),mean_MFE_ATR=st.mean(r['mfe'] for r in xs),mean_MAE_ATR=st.mean(r['mae'] for r in xs),race=dict(collections.Counter(r['race'] for r in xs)))
        qs=[m for m in meta.values() if m.get('family')==fam and m['period']!='july']
        delay[fam]=dict(n=len(qs),same_seed=sum(m['delay_bars']==0 for m in qs),median_bars=st.median(m['delay_bars'] for m in qs))
    cash=lambda r:t.EV(r)['net_amount'];base=rows['TOUCH_BTRUE'];attrib={}
    for m in t.NEW:
        a=rows[m];common=a.keys()&base.keys();added=a.keys()-base.keys();removed=base.keys()-a.keys()
        q=dict(common_n=len(common),common_net_delta=sum(cash(a[k])-cash(base[k]) for k in common),added_n=len(added),added_net=sum(cash(a[k]) for k in added),removed_n=len(removed),removed_original_net=sum(cash(base[k]) for k in removed))
        assert abs(sum(map(cash,a.values()))-sum(map(cash,base.values()))-q['common_net_delta']-q['added_net']+q['removed_original_net'])<1e-6;attrib[m]=q
        groups=collections.defaultdict(list)
        for r in a.values():groups['same_seed' if meta[r['event_key']]['delay_bars']==0 else 'post_seed'].append(cash(r))
        same_vs_later[m]={g:dict(n=len(groups[g]),net=sum(groups[g])) for g in ['same_seed','post_seed']}
    t.write('funnel.json',{k:dict(v) for k,v in funnel.items()})
    t.write('comparison-summary.json',dict(models=summary,screen_pass=passes,paths=path_summary,delays=delay,retrospective_same_seed_vs_post_seed=same_vs_later))
    t.write('parent-attribution.json',attrib)
    schedule=R(RUN/'chronological-selection.json');cutoff=R(RUN/'july-cutoff-selection.json')
    lines=['# 鸡蛋ABC：B尾端转向确认验证','',
        '2026-10-26。主历史保留231个合格ABC预警，测试4类确认×2种入场，共8个新方案；另有3个完整对照。ABC识别、原S1目标与原预警时整个B段止损均冻结，生产策略未改。','',
        '## 实验定义与边界','',
        '|确认|固定定义|','|---|---|',
        '|连续两柱转向|原预警之后至少完成两根15分钟柱；最近三柱不再顺B创新极值，收盘连续向A方向移动|',
        '|三柱局部转折|中柱构成局部B方向极值，右柱不再创新极值，且右柱收盘越过中柱的反向极值|',
        '|突破前三柱区间|收盘越过前三柱向A方向的极值，同时本柱不再顺B创新极值|',
        '|三类最先确认|上述任一条件第一次成立；不拟合权重|','',
        '每类分别在确认后下一分钟开盘成交，或在确认柱向A方向极值外1tick触发。局部转折和区间突破允许原预警柱本身完成确认，连续两柱规则必须等待；这一差别事前写入协议。','',
        '观察原预警柱及其后最多12根完成柱；确认前碰到原S1或B止损即取消，确认不能跨合约或评分窗口。最近四柱必须同实际合约、标签唯一、都有完整15根正成交量分钟且OHLC与识别数据一致。前面三柱须位于S1之后。每个预警取首个确认，同A/同类/同确认时刻取最早父预警，不按后续收益去重。','',
        '原预警只是观察，不占订单位置；确认时才开始挂单和占仓。入场后保留原净盈亏比、费用空间、最小风险、追价、节假日、最长持有等约束，确认不重置止损缓冲。各方案连续执行一单/一仓及同A两次已实现止损限制，不能相加。','',
        '这些都是旧历史上的追加探索，8个方案不构成独立市场样本。7月13—24日上轮已看过，本轮明确作为已知失败区间复核。没有把它重新命名为未触碰样本外。','',
        '## 主历史结果','',
        '覆盖2026年1—6月、7月1—10日、8月10日—10月23日11:05；缺口不补造交易。金额为一手扣冻结手续费及滑点后的元。归一化是假设每笔计划风险300元的研究值，非账户收益；回撤为平仓曲线回撤，未计逐分钟浮亏。','',
        '|方案|平仓数|主净额|8场景最低净额|归一净额|最低归一|最多未知|','|---|---:|---:|---:|---:|---:|---:|']
    for m in t.MODELS:
        q=summary[m];v=q['primary'];lines.append(f"|{title(m)}|{v['n']}|{f(v['net'])}|{f(q['min8net'])}|{f(v['normalized_net'])}|{f(q['min8normalized'])}|{q['max8unknown']}|")
    lines+=['','|方案|Q1|Q2|Q3覆盖段|10月部分|删除最佳3笔后归一净额|','|---|---:|---:|---:|---:|---:|']
    for m in t.MODELS:
        q=summary[m]['primary'];lines.append('|'+title(m)+'|'+'|'.join(f(q['blocks'][n]['net']) for n in ['Q1_train','Q2','Q3','October_partial'])+'|'+f(q['normalized_ex_top3'])+'|')
    lines+=['','8个新方案均未达到“至少60笔、所有场景现金和归一收益为正、删除最佳3笔后归一为正、至少3个时间块为正、无未知”的完整门槛。局部转折＋极值触发12笔净赚1,251元，但删除最佳3笔后归一亏906元；不能当作稳定高频方案。原C15对照也仅14笔且利润集中，仍是研究观察。','',
        '## 低频发生在什么环节','',
        '|确认类|可形成确认的去重预警数|原预警柱已确认|确认延迟中位数/柱|','|---|---:|---:|---:|']
    for fam,q in delay.items():lines.append(f"|{F[fam]}|{q['n']}|{q['same_seed']}|{f(q['median_bars'])}|")
    anyledger=[r for r in ledger if r['period']!='july' and r['family']=='ANY'];reason=collections.Counter(r['reason'] for r in anyledger)
    h=sum((funnel[per+'/ANY_OPEN'] for per in ['early','recent']),collections.Counter())
    lines+=['',f"合并确认从231个父预警形成{reason['confirmed']}个去重确认，确认前先破B止损{reason['B_invalidated']}个、先碰S1 {reason['S1_reached']}个，其余为重复或未确认。进入开盘方案后：计划净盈亏比拒绝{h['filtered_net_rr_plan']}个、费用空间拒绝{h['filtered_entry_economics']}个、风险/摩擦几何拒绝{h['filtered_geometry']}个；独立回放平仓{h['closed']}个，连续占仓竞争后32个。",'约束多在下单资格与收益空间环节发生，不能靠解除占仓限制解决。三类合并本身没有增加到60笔；继续等待确认还可能消耗S1之前的空间。','',
        '## 确认后的价格方向','',
        '从确认柱收盘观察未来16根完整15分钟柱，统一按父预警时ATR14计量。不同确认的窗口和条件不同，不能将均值差直接解释为确认的因果增益；未按成交盈亏筛选。','',
        '|确认类|完整路径数|平均方向收益ATR|平均最大有利ATR|平均最大不利ATR|先+1 / 先−1 / 同分钟未知|','|---|---:|---:|---:|---:|---|']
    for fam,q in path_summary.items():
        race=q['race'];lines.append(f"|{F[fam]}|{q['n']}|{f(q['mean_close_ATR'])}|{f(q['mean_MFE_ATR'])}|{f(q['mean_MAE_ATR'])}|{race.get('upper_first',0)} / {race.get('stop_first',0)} / {race.get('same_minute_unknown',0)}|")
    lines+=['','区间突破组平均16柱方向收益约+0.43ATR，显示部分条件可能有方向信息；但实际开盘仅18笔、净额+248元，加大执行压力转亏。局部转折组约+0.04ATR，几乎没有净方向推进。预测方向、可成交盈亏比与稳定收益是三个不同要求。','',
        '|方案|原预警柱确认：平仓数/净额|后续柱确认：平仓数/净额|','|---|---:|---:|']
    for m,g in same_vs_later.items():lines.append(f"|{title(m)}|{g['same_seed']['n']} / {f(g['same_seed']['net'])}|{g['post_seed']['n']} / {f(g['post_seed']['net'])}|")
    lines+=['','该分组是结果后的描述，未删除原预警柱确认后重竞争，也没有据此创建“仅后续确认”新赢家。合并开盘的后续确认12笔仍亏775元，不能把总亏损都归因于同柱确认。','',
        '## 时间迁移与已知失败窗口','',
        '选择器每季度只读截止日前严格平仓的训练收益，要求20笔、每覆盖日0.35笔、所有压力归一收益为正、去最佳笔后为正、各场景不低于E20S1；无合格新方案则沿用研究对照。无季度清仓重置。','']
    lines.extend('- '+q['start'][:7]+'：'+title(q['chosen']) for q in schedule)
    lines+=['',f"7月13日时间截止诊断同样选择{title(cutoff['chosen'])}。这次诊断发生在已经知道该段历史失败之后，不能视为前瞻选模。7月窗口复用已核对的固定JD2608分钟数据，带入7月13日前订单状态。",'',
        '|方案|7月13—24日平仓数|净额|8场景最低净额|','|---|---:|---:|---:|']
    for m in t.NEW+['TOUCH_BTRUE','E20S1']:
        q=july[primary+'/'+m];lines.append(f"|{title(m)}|{q['n']}|{f(q['net'])}|{f(min(july[c+'/'+m]['net'] for c in cases))}|")
    lines+=['','大部分新规则在失败区间没有成交；局部转折开盘和合并开盘各1笔，均亏374元。没有成交只能说明暂未暴露，不能当作预测正确。原E20S1该段2笔净赚93元，但压力最低亏247元。','',
        '含该窗口重新连续竞争的[合并结果](combined-results.json)另存；C15未在该固定合约窗口生成新候选，因此其合并值不作为完整覆盖对照。','',
        '## 本轮判断','',
        '1. 简单的“停止创新极值＋完成柱转向”未能把鸡蛋变成高频、稳定的趋势延续策略；宽松合并反而放进更多亏损机会。',
        '2. 较严格的“先推进、再回踩确认”仍有低频盈利样本，但无法支持大几十笔或达到EG一半收益的要求，不能按总盈利外推。',
        '3. 下一轮可改变ABC的用途：把它作为结构背景，研究延续失败后是否存在反向扩展，而不是继续叠加同方向确认。先定义结构级失效（例如A起点失守）再检验反向机会，不能每次小止损就机械反手。该方向尚未验证。','',
        'EG/PVC完整B5保持原定义。此次没有新方案升级，生产代码未改。','',
        '证据：[冻结协议](protocol.json) · [全部场景](main-results.json) · [确认/取消明细](early/confirmation-ledger.json) · [成交漏斗](funnel.json) · [父预警归因](parent-attribution.json) · [确认后路径](confirmation-paths.json) · [完成核验](completion.json)。','',
        '工程记录：v1在读取第一批确认柱时因缺少成交量字段终止，未产生任何有效个体评价；v2补齐真实分钟聚合成交量及持仓量，确认规则与生成键不变。失败档案保留，未覆盖旧结果。','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8');plot(summary)
    verification=verify(p,sourcehash)
    t.write('completion.json',dict(completed=True,production_changed=False,untouched_OOS=False,screen_pass=passes,verification=verification,
        native_hashes=sourcehash,report_sha256=t.sha(RUN/'summary.md'),report_script_sha256=t.sha(__file__),
        artifact_hashes={str(q.relative_to(RUN)):t.sha(q) for q in RUN.rglob('*.json') if q.name!='completion.json'}))
    print(json.dumps(dict(passes=passes,main={m:dict(n=summary[m]['primary']['n'],net=summary[m]['primary']['net'],worst=summary[m]['min8net']) for m in t.MODELS}),ensure_ascii=False))

def verify(p,sourcehash):
    assert p['script_sha256']==t.sha(t.__file__)
    assert all(t.sha(t.b.ROOT/q)==h for q,h in p['production_before'].items())
    assert all(t.sha(q)==h for q,h in p['dependencies'].items())
    assert t.sha(p['native_binary']['path'])==p['native_binary']['sha256']
    frozen=R(t.b.RUN/'generation-checks.json')['source_hashes']
    oldpaths=R(t.b.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')['periods']
    for period in ['early','recent']:
        for k in ['input','contracts','segments','fees','holidays']:assert t.sha(oldpaths[period][k])==frozen[period][k]
    archived={q.replace('\\','/'):h for q,h in R(t.w.RUN/'completion.json')['artifacts'].items()}
    for q in ['supplement/input.json','supplement/contracts.json','supplement/segments.json','supplement/qualified-seeds.json']:
        assert t.sha(t.w.RUN/q)==archived[q]
    native_n=closed=0
    for q,h in sourcehash.items():
        assert t.sha(RUN/q)==h
        for line in (RUN/q).open(encoding='utf-8'):
            r=json.loads(line);native_n+=1
            for fill in r['result']['fills']:
                assert fill['ts']>r['warning_ts'];assert abs(fill['price']/r['tick_size']-round(fill['price']/r['tick_size']))<1e-6
            if t.EV(r)['status']=='closed':
                closed+=1;ev=t.EV(r);sign=1 if r['direction']=='up' else -1
                assert abs((ev['fill']-r['stop'])*sign-ev['risk'])<1e-6;assert ev['risk']>=10-1e-6
                assert ev['exit_ts']>=ev['entry_ts']>r['warning_ts']
    for period in ['early','recent']:
        old=t.b.ROOT/'research-runs/swing-jd-btail-confirmation-20261026-v1'/period
        for m in t.NEW:
            current=R(RUN/period/m/(period+'-pool.json'));before=R(old/m/(period+'-pool.json'))
            assert [s['event_key'] for s in current]==[s['event_key'] for s in before]
            assert all(a['event']==b['event'] for a,b in zip(current,before))
    return dict(native_rows=native_n,closed_native_rows=closed,strict_entry_after_decision=True,tick_grid=True,actual_risk_reconciled=True,
        production_unchanged=True,causal_prefix_checks=True,v1_to_v2_signal_keys_and_events_unchanged=True,python_mechanism_tests=6,
        old_control_exact_native_rows=R(RUN/'main-verification.json')['exact_native_controls'])

def plot(summary):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif']=['Microsoft YaHei'];plt.rcParams['axes.unicode_minus']=False
    fig,axs=plt.subplots(1,2,figsize=(16,6),gridspec_kw={'width_ratios':[1.3,1]});names=t.MODELS[::-1]
    for i,m in enumerate(names):
        q=summary[m];axs[0].plot([q['min8net'],q['primary']['net']],[i,i],c='#a0b2c4');axs[0].scatter(q['primary']['net'],i,c='#258574' if q['primary']['net']>=0 else '#b04c43',s=30)
    axs[0].set_yticks(range(len(names)),[title(m) for m in names],fontsize=9);axs[0].axvline(0,c='#999999',lw=.8);axs[0].set_xlabel('一手净额（元），线段左端为8场景最低值');axs[0].set_title('8个新方案与3个完整对照')
    for m in ['E20S1','C15_BTRUE','PIVOT3_TOUCH','ANY_OPEN']:
        q=summary[m]['primary'];months=list(q['month_cash']);total=0;ys=[]
        for mo in months:total+=q['month_cash'][mo];ys.append(total)
        axs[1].plot(months,ys,marker='o',ms=3,label=title(m))
    axs[1].axhline(0,c='#999999',lw=.8);axs[1].tick_params(axis='x',rotation=55);axs[1].set_title('按预警月份归属的累计已平仓净额');axs[1].legend(fontsize=8);axs[1].set_ylabel('元')
    fig.tight_layout();fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)

def finalize_existing():
    # Resume after a reporting-only path-key normalization error; preserve all
    # already-written numerical evidence, report and image byte-for-byte.
    p=R(RUN/'protocol.json');sourcehash={}
    for period in ['early','recent','july']:
        for m in t.NEW+(['TOUCH_BTRUE','E20S1'] if period=='july' else []):
            path=RUN/period/m/'executions/evaluations.jsonl';sourcehash[str(path.relative_to(RUN))]=t.sha(path)
    verification=verify(p,sourcehash)
    t.write('completion.json',dict(completed=True,production_changed=False,untouched_OOS=False,
        screen_pass=R(RUN/'comparison-summary.json')['screen_pass'],verification=verification,native_hashes=sourcehash,
        report_sha256=t.sha(RUN/'summary.md'),report_script_sha256=t.sha(__file__),
        artifact_hashes={str(q.relative_to(RUN)):t.sha(q) for q in RUN.rglob('*.json') if q.name!='completion.json'}))
    print(json.dumps(verification))

if __name__=='__main__':
    import sys
    if '--finalize-existing' in sys.argv:finalize_existing()
    else:run()
