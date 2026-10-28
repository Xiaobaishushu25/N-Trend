"""Report all fixed variants, concentration, chronology and supplementary failures."""
import collections
import json
import statistics as st
import research_swing_jd_warning_quality as w

RUN=w.RUN
R=w.R
T={'BASE':'全部','FRESH':'剩余空间≥一半','ORDERLY':'B回撤有序','CLEAN':'A推动干净','HOUR':'小时同向','FIRST':'每个A首个预警'}
def title(m):
    if m=='TOUCH_BTRUE':return '原触发＋整个B止损'
    if m=='E20S1':return '原S1＋成本20%'
    entry,g=m.split('_');return ('预警次分钟开盘' if entry=='EARLY' else '预警实体中点回踩')+'／'+T[g]
def f(x):return '—' if x is None else f'{x:,.2f}'

def run():
    p=R(RUN/'protocol.json');primary=p['primary'];cases=w.cases(p);fixed=R(RUN/'fixed-results.json');supp=R(RUN/'supplement/fixed-results.json');paths=R(RUN/'warning-path-diagnostic.json')
    summary={};passed=[]
    for m in w.MODELS:
        q=fixed[primary+'/'+m];xs=[fixed[c+'/'+m] for c in cases]
        flags=dict(n60=q['n']>=60,positive_cash_all8=min(z['net'] for z in xs)>0,positive_normalized_all8=min(z['normalized_net'] for z in xs)>0,
            ex_top3_positive=q['normalized_ex_top3']>0,three_positive_blocks=sum(z['net']>0 for z in q['blocks'].values())>=3,no_unknown=max(z['unknown'] for z in xs)==0)
        summary[m]=dict(primary=q,min8net=min(z['net'] for z in xs),min8normalized=min(z['normalized_net'] for z in xs),max8unknown=max(z['unknown'] for z in xs),flags=flags)
        if m in w.NEW and all(flags.values()):passed.append(m)
    # All new native rows, including immediate filters, preserve the frequency funnel.
    funnel=collections.defaultdict(collections.Counter);sourcehash={}
    for period in ['early','recent','supplement']:
        for m in w.NEW+(w.MODELS[-2:] if period=='supplement' else []):
            path=RUN/period/m/'executions/evaluations.jsonl';sourcehash[str(path.relative_to(RUN))]=w.sha(path)
            for line in path.open(encoding='utf-8'):
                r=json.loads(line)
                if r['scenario'].split('/',1)[1]==primary.split('/',1)[1]:funnel[period+'/'+m][w.EV(r)['status']]+=1
    w.write('funnel.json',{k:dict(v) for k,v in funnel.items()})
    blockpaths={}
    for g in w.GROUPS:
        blockpaths[g]={}
        for block in w.b.z.x.blocks():
            rows=[r for r in paths['rows'] if r['complete'] and r['groups'][g] and block['start']<=r['warning_ts']<block['end']]
            blockpaths[g][block['label']]=dict(n=len(rows),mean_close_ATR=st.mean(r['end_r'] for r in rows) if rows else None,
                race=dict(collections.Counter(r['race'] for r in rows)))
    w.write('comparison-summary.json',dict(models=summary,screen_pass=passed,block_warning_paths=blockpaths))
    trades=collections.defaultdict(dict)
    for line in (RUN/'accepted-primary.jsonl').open(encoding='utf-8'):
        x=json.loads(line);r=x['row']
        if w.EV(r)['status']=='closed':trades[x['model']][r['event_key']]=r
    _,_,native,_=w.load();sf=primary.split('/',1)[1];original={r['event_key']:r for r in native['TOUCH_BTRUE',sf]}
    attribution={};base=trades['TOUCH_BTRUE'];cash=lambda r:w.EV(r)['net_amount']
    for m in ['EARLY_BASE','PULLBACK_BASE','PULLBACK_FRESH']:
        xs=trades[m];common=xs.keys()&base.keys();added=xs.keys()-base.keys();removed=base.keys()-xs.keys();status=collections.defaultdict(list)
        for k in added:status[w.EV(original[k])['status']].append(cash(xs[k]))
        q=dict(common_n=len(common),common_net_delta=sum(cash(xs[k])-cash(base[k]) for k in common),added_n=len(added),added_net=sum(cash(xs[k]) for k in added),
            removed_n=len(removed),removed_old_net=sum(cash(base[k]) for k in removed),added_original_status={s:dict(n=len(v),net=sum(v)) for s,v in status.items()})
        assert abs(sum(map(cash,xs.values()))-sum(map(cash,base.values()))-q['common_net_delta']-q['added_net']+q['removed_old_net'])<1e-6
        attribution[m]=q
    w.write('entry-attribution.json',attribution)
    chosen=R(RUN/'supplement-decision-before-test.json')['chosen'];dat=R(RUN/'supplement/data-checks.json');schedule=R(RUN/'selection-before-test.json')
    lines=['# 鸡蛋ABC：预警质量、提前入场与补充时段验证','',
        '2026-10-26。保留ABC识别、评分、原S1目标与整个B段止损；8个预先固定新方案，另保留2个完整对照。没有修改生产策略或EG/PVC/SR规则。','',
        '这是顺序探索：同一批历史已研究多次，季度训练/测试只能检查时间迁移，不能重新称为未触碰样本。补充7月时段在生成其信号和成交前保存了选择结果，但来自既有归档、合约口径也有变化，证据仍有限。','',
        '## 预先固定的改变','',
        '- 提前入场：ABC预警完成后，以预警收盘作计划价，实际在下个观察到的分钟开盘成交，计不利滑点；实际成交重新检查净盈亏比、风险和追价。延迟场景再晚一分钟。',
        '- 实体中点回踩：预警开收中点按tick向收盘方向取整，至少比收盘有利1tick才挂单；无需先突破旧触发位。触及S1在先则取消，真实同分钟顺序冲突保留未知。',
        '- 两种方式都保留整个B极值外原缓冲止损、原S1目标、至少10tick风险、名义摩擦≤0.20R、费用/目标空间≤20%、计划及成交净盈亏比≥1、60根最长持有与原节假日处理。',
        '- 6个开盘方案分别使用全部、剩余空间≥一半、B回撤有序、A推动干净、小时同向、每个A首个合格预警；回踩只测全部与剩余空间。没有阈值网格、事后组合筛选。所有定义见[冻结协议](protocol.json)。','',
        '## 主历史：实际分钟成交','',
        '覆盖2026年1—6月、7月1—10日、8月10日—10月23日11:05，缺口不作零机会。金额均为一手扣冻结手续费及滑点后的元；归一化每笔计划风险300元仅供研究比较，不是账户收益。8场景是成交方式×滑点×未知持仓处理，不是8份独立市场样本。','',
        '|方案|平仓数|主净额|8场景最低净额|归一净额|8场景最低归一|最多未知|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for m in w.MODELS:
        q=summary[m];v=q['primary'];lines.append(f"|{title(m)}|{v['n']}|{f(v['net'])}|{f(q['min8net'])}|{f(v['normalized_net'])}|{f(q['min8normalized'])}|{q['max8unknown']}|")
    lines+=['','|方案|Q1|Q2|Q3覆盖段|10月部分|去最佳3笔后归一|归一已实现回撤|','|---|---:|---:|---:|---:|---:|---:|']
    for m in w.MODELS:
        q=summary[m]['primary'];lines.append('|'+title(m)+'|'+'|'.join(f(q['blocks'][k]['net']) for k in ['Q1_train','Q2','Q3','October_partial'])+'|'+f(q['normalized_ex_top3'])+'|'+f(q['normalized_realized_dd'])+'|')
    lines+=['','这里的回撤是平仓收益曲线回撤，未计未平仓逐分钟浮亏。各方案分别运行一单/一仓和同A两次已实现止损限制，不能把收益直接相加。未知不按0元盈利填补。',
        '提前开盘的“全部”和“剩余空间”成交完全相同：原计划净盈亏比门槛已排除了空间不足者，该条件没有提供额外方向信息。','',
        '## 为什么提前入场反而变差','',
        '|方案对比原触发＋整个B止损|共同预警数|共同预警净额变化|新增平仓数|新增净额|消失平仓数|消失交易原净额|','|---|---:|---:|---:|---:|---:|---:|']
    for m,q in attribution.items():lines.append(f"|{title(m)}|{q['common_n']}|{f(q['common_net_delta'])}|{q['added_n']}|{f(q['added_net'])}|{q['removed_n']}|{f(q['removed_old_net'])}|")
    lines+=['',
        '提前开盘共同23个预警合计多赚890元，但新增26笔合计亏3,052元。其中6个预警在原触发方式下先破止损、根本未触发，提前入场后亏2,259元；16个原本过不了计划净盈亏比的预警，提前后虽然可以下单，仍合计亏421元。回踩新增25笔合计亏3,235元。',
        '因此提前成交确实改善部分已有机会的价位，但也把未经价格确认的结构变成了订单。新增还受占仓路径影响，不能全部解释为触发条件的纯因果效果；逐类原状态保存在[入场归因](entry-attribution.json)。','',
        '## 预警本身有无方向优势','',
        '以下从同一个原预警收盘开始，不受入场成交、止损宽度或事后盈利筛选影响；以当时ATR14为单位，观察随后16根完整15分钟柱。原始231个合格预警全部纳入，包含不能下单者。类别相互重叠，不能把样本数相加。','',
        '|预警组|总数/完整路径|平均16柱方向收益ATR|平均最大有利ATR|平均最大不利ATR|先+1ATR / 先-1ATR / 同分钟未知|','|---|---:|---:|---:|---:|---|']
    for g in w.GROUPS:
        q=paths['groups'][g];race=q['race'];lines.append(f"|{T[g]}|{q['total']}/{q['complete']}|{f(q['mean_close_ATR'])}|{f(q['mean_MFE_ATR'])}|{f(q['mean_MAE_ATR'])}|{race.get('upper_first',0)} / {race.get('stop_first',0)} / {race.get('same_minute_unknown',0)}|")
    lines+=['','|预警组|Q1方向ATR均值(样本)|Q2|Q3覆盖段|10月部分|','|---|---|---|---|---|']
    for g in w.GROUPS:
        lines.append('|'+T[g]+'|'+'|'.join(f"{f(blockpaths[g][k]['mean_close_ATR'])} ({blockpaths[g][k]['n']})" for k in ['Q1_train','Q2','Q3','October_partial'])+'|')
    lines+=['','这些是条件描述，不是显著性检验；整体均值可能受方向和行情阶段影响，跨时间同号与实际成本压力更值得关注。','',
        '## 严格截止日选择','',
        '每次只能读取截止日前已严格平仓的训练收益；至少20笔、每覆盖日0.35笔、所有场景归一收益为正、去最佳笔后为正、每场景不低于原E20S1。未达标沿用E20S1研究对照。连续占仓不按季度清空。','']
    lines.extend('- '+q['start'][:7]+'：'+title(q['chosen']) for q in schedule)
    lines+=['','## 7月补充评分窗口','',
        f"固定7月13—24日，共{len(dat['score_days'])}个实际报价日；使用7月10日已知主力JD2608，分钟归档延伸至7月28日。完整1分钟生成5分钟数据，预热自6月12日；丢弃{dat['incomplete_groups_dropped']}个不足5分钟分组，没有补造报价。窗口内零成交量分钟{dat['scored_zero_volume_minutes']}个，真实执行如遇零量按未知。",
        f"7月1—10日与原连续主力已有警报重叠核对：旧{dat['overlap_old']}个、新{dat['overlap_new']}个，共有{dat['overlap_common']}个，其中{dat['overlap_exact_geometry']}个几何锚点完全一致。固定合约预热长度、连续主力历史切换及后续换月口径不同，新增时段单列。",
        f"新窗口合格ABC预警{dat['qualified_seeds']}个；预先选择：**{title(chosen)}**。以下完整列出每个固定方案，不据此重选。每个方案带入主历史7月13日前订单状态，按补充窗口预警归属统计，未重置占仓。",
        '', '|方案|补充平仓数|净额|8场景最低净额|最多未知|','|---|---:|---:|---:|---:|']
    for m in w.MODELS:
        q=supp[primary+'/'+m];xs=[supp[c+'/'+m] for c in cases];lines.append(f"|{title(m)}|{q['n']}|{f(q['net'])}|{f(min(z['net'] for z in xs))}|{max(z['unknown'] for z in xs)}|")
    lines+=['','## 判断与下一步','',
        '满足本轮“≥60笔、所有场景现金及归一收益为正、去最佳3笔后归一为正、至少3个时间块为正、无未知”的新方案：'+('、'.join(title(m) for m in passed) if passed else '**没有**')+'。这只是历史筛选门槛，通过也不能直接推广。','',
        '本轮更支持“原触发价格确认有筛选作用”，而不支持“只要更早、更便宜入场就能补足收益”。全体预警先+1ATR为117次、先-1ATR为110次，方向优势很薄；A质量、B回撤、小时方向和首个预警这些直观筛选，也未同时给出频率、成本韧性和跨时段稳定性。',
        '接下来值得保留的候选是“ABC作为背景，另定义B尾端转向的触发与失效”：例如先停止顺B创新极值，再用完成柱确认转向；同时记录未确认、确认前失效和确认后的全部机会。它需要新的冻结实验，目前尚未验证。此前C确认样本少，也不能据此直接扩大到60笔。',
        'EG/PVC完整B5继续保留；鸡蛋这批新规则均不升级。先积累冻结规则后的新时段，检验触发本身的增量价值，再决定是否进入观察交易。','',
        '证据：[完整场景](fixed-results.json) · [逐预警路径](warning-path-diagnostic.json) · [成交漏斗](funnel.json) · [补充前选择](supplement-decision-before-test.json) · [补充场景](supplement/fixed-results.json)。','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8')
    plot(summary)
    assert all(w.sha(w.b.ROOT/q)==h for q,h in p['production_before'].items())
    assert p['script_sha256']==w.sha(w.__file__)
    assert all(w.sha(RUN/'fork'/q)==h for q,h in R(RUN/'implementation-manifest.json').items())
    assert dat['decision_sha256']==w.sha(RUN/'supplement-decision-before-test.json')
    for q in fixed.values():assert abs(sum(q['month_cash'].values())-q['net'])<1e-6
    w.write('completion.json',dict(completed=True,production_changed=False,untouched_OOS=False,screen_pass=passed,
        native_source_hashes=sourcehash,report_sha256=w.sha(RUN/'summary.md'),
        artifacts={str(q.relative_to(RUN)):w.sha(q) for q in RUN.rglob('*.json') if 'fork' not in q.parts and q.name!='completion.json'}))
    print(json.dumps(dict(passes=passed,supplement_chosen=chosen,summary={m:dict(n=q['primary']['n'],net=q['primary']['net'],worst=q['min8net']) for m,q in summary.items()}),ensure_ascii=False))

def plot(summary):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif']=['Microsoft YaHei'];plt.rcParams['axes.unicode_minus']=False
    fig,axes=plt.subplots(1,2,figsize=(15,6),gridspec_kw={'width_ratios':[1.1,1]})
    names=w.MODELS[::-1];ax=axes[0]
    for i,m in enumerate(names):
        q=summary[m];ax.plot([q['min8net'],q['primary']['net']],[i,i],color='#9aabc0');ax.scatter(q['primary']['net'],i,c='#237d6c' if q['primary']['net']>=0 else '#b34f43',s=35)
    ax.set_yticks(range(len(names)),[title(m) for m in names],fontsize=9);ax.axvline(0,c='#999999',lw=.8);ax.set_xlabel('一手净额（元）；线段另一端为8场景最低值');ax.set_title('全部固定方案，完整保留失败结果')
    ax=axes[1]
    for m,color in [('E20S1','#666666'),('TOUCH_BTRUE','#2878a3'),('EARLY_BASE','#ce7920'),('PULLBACK_BASE','#258c76')]:
        q=summary[m]['primary'];months=list(q['month_cash']);ys=[];total=0
        for month in months:total+=q['month_cash'][month];ys.append(total)
        ax.plot(months,ys,marker='o',ms=3,label=title(m),color=color)
    ax.axhline(0,c='#999999',lw=.8);ax.tick_params(axis='x',rotation=55);ax.set_ylabel('累计一手已平仓净额（元）');ax.set_title('按预警月份归属；缺失区间无新增评分');ax.legend(fontsize=8)
    fig.tight_layout();fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)

if __name__=='__main__':run()
